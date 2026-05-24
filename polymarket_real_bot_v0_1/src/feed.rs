use anyhow::{Context, Result};
use futures_util::StreamExt;
use rust_decimal::Decimal;
use rust_decimal::prelude::{FromStr, ToPrimitive};
use serde::Deserialize;
use std::collections::VecDeque;
use tokio::sync::watch;
use tokio_tungstenite::{connect_async, tungstenite::Message};
use tracing::{error, info, warn};

#[derive(Debug, Clone, Deserialize)]
struct BinanceTicker {
    #[serde(rename = "c")]
    last_price: String,
}

/// A single price observation with its timestamp.
#[derive(Debug, Clone, Copy)]
struct PricePoint {
    price: Decimal,
    ts: u64, // unix millis
}

/// Shared state between the WS task and the feed.
/// Tracks a rolling window of prices for momentum calculation.
pub struct BinanceFeed {
    rx: watch::Receiver<Decimal>,
    /// Rolling price history (newest last).
    history: VecDeque<PricePoint>,
    /// How many seconds of history to keep for momentum.
    momentum_window_secs: u64,
}

impl BinanceFeed {
    pub async fn new(symbol: &str) -> Result<Self> {
        let (tx, rx) = watch::channel(Decimal::ZERO);

        let stream_symbol = symbol.to_lowercase();
        let url = format!(
            "wss://stream.binance.com:9443/ws/{stream_symbol}@ticker"
        );

        info!(symbol = %symbol, "starting Binance feed");

        tokio::spawn(async move {
            loop {
                if let Err(e) = run_ws_loop(&url, &tx).await {
                    error!(error = %e, "Binance WS error, reconnecting in 5s");
                }
                tokio::time::sleep(std::time::Duration::from_secs(5)).await;
                info!("reconnecting Binance feed");
            }
        });

        Ok(Self {
            rx,
            history: VecDeque::new(),
            momentum_window_secs: 900, // 15-minute window for scalping
        })
    }

    pub fn latest_price(&self) -> Decimal {
        *self.rx.borrow()
    }

    /// Returns the latest price.
    pub fn price(&mut self) -> Decimal {
        let p = *self.rx.borrow();
        if p > Decimal::ZERO {
            let now = now_millis();
            self.history.push_back(PricePoint { price: p, ts: now });
            self.trim_history();
        }
        p
    }

    /// Compute the % change over the momentum window.
    /// Returns (pct_change, signal) where:
    ///   pct_change = (current - oldest_in_window) / oldest_in_window
    ///   signal = normalized probability (0.5 = neutral, >0.5 = bullish, <0.5 = bearish)
    ///
    /// The signal maps momentum to a probability:
    ///   +2% over 5min → signal ~0.70 (strongly bullish)
    ///   +1% → signal ~0.60
    ///    0% → signal ~0.50 (neutral)
    ///   -1% → signal ~0.40
    ///   -2% → signal ~0.30 (strongly bearish)
    pub fn momentum_signal(&mut self) -> (f64, f64) {
        self.rx.borrow(); // ensure latest is read
        let p = *self.rx.borrow();
        if p <= Decimal::ZERO {
            return (0.0, 0.5);
        }

        let now = now_millis();
        self.history.push_back(PricePoint { price: p, ts: now });
        self.trim_history();

        if self.history.len() < 2 {
            return (0.0, 0.5);
        }

        let current = self.history.back().unwrap().price;
        let oldest = self.history.front().unwrap().price;

        if oldest <= Decimal::ZERO {
            return (0.0, 0.5);
        }

        let pct_change = ((current - oldest) / oldest).to_f64().unwrap_or(0.0);

        // Map % change to probability signal:
        // +1% → 0.60, +2% → 0.70, -1% → 0.40, -2% → 0.30
        let signal: f64 = 0.5 + pct_change * 10.0;
        let signal: f64 = signal.clamp(0.10, 0.90);

        (pct_change, signal)
    }

    fn trim_history(&mut self) {
        let cutoff = now_millis() - (self.momentum_window_secs * 1000);
        while self.history.front().map_or(false, |p| p.ts < cutoff) {
            self.history.pop_front();
        }
    }
}

fn now_millis() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64
}

async fn run_ws_loop(url: &str, tx: &watch::Sender<Decimal>) -> Result<()> {
    let (ws_stream, _) = connect_async(url)
        .await
        .context("failed to connect to Binance WS")?;

    info!("Binance WS connected");

    let (_, mut read) = ws_stream.split();

    while let Some(msg) = read.next().await {
        match msg {
            Ok(Message::Text(text)) => {
                if let Ok(ticker) = serde_json::from_str::<BinanceTicker>(&text) {
                    if let Ok(price) = Decimal::from_str(&ticker.last_price) {
                        let _ = tx.send(price);
                    }
                }
            }
            Ok(Message::Ping(_data)) => {
                // pong handled automatically
            }
            Ok(Message::Close(_)) => {
                warn!("Binance WS closed by server");
                break;
            }
            Err(e) => {
                error!(error = %e, "Binance WS read error");
                break;
            }
            _ => {}
        }
    }

    Ok(())
}