use anyhow::{Context, Result};
use futures_util::StreamExt;
use rust_decimal::Decimal;
use rust_decimal::prelude::{FromStr, ToPrimitive};
use serde::Deserialize;
use std::collections::VecDeque;
use tokio::sync::watch;
use tokio_tungstenite::{connect_async, tungstenite::Message};
use tracing::{debug, error, info, warn};

// ── indicator constants ──────────────────────────────────────

const RSI_PERIOD: usize = 14;
const MACD_FAST: usize = 12;
const MACD_SLOW: usize = 26;
const MACD_SIGNAL: usize = 9;
const BB_PERIOD: usize = 20;
const BB_STDDEV: f64 = 2.0;

const W_MOMENTUM: f64 = 0.20;
const W_RSI: f64 = 0.25;
const W_MACD: f64 = 0.25;
const W_BB: f64 = 0.30;

const VOLUME_EMA_ALPHA: f64 = 0.1;
const SIGNAL_MIN: f64 = 0.10;
const SIGNAL_MAX: f64 = 0.90;
const SIGNAL_NEUTRAL: f64 = 0.50;

const EMA_FAST_MULT: f64 = 2.0 / (MACD_FAST as f64 + 1.0);
const EMA_SLOW_MULT: f64 = 2.0 / (MACD_SLOW as f64 + 1.0);
const EMA_SIGNAL_MULT: f64 = 2.0 / (MACD_SIGNAL as f64 + 1.0);

// ── data structures ──────────────────────────────────────────

#[derive(Debug, Clone, Deserialize)]
struct BinanceTicker {
    #[serde(rename = "c")]
    last_price: String,
    #[serde(rename = "v")]
    base_volume: String,
    #[serde(rename = "q")]
    quote_volume: String,
    #[serde(rename = "n")]
    num_trades: u64,
    #[serde(rename = "o")]
    open_price: String,
    #[serde(rename = "h")]
    high_price: String,
    #[serde(rename = "l")]
    low_price: String,
    #[serde(rename = "P")]
    price_change_pct: String,
}

/// What gets sent through the watch channel each tick.
#[derive(Debug, Clone)]
struct TickerUpdate {
    price: Decimal,
    volume: f64, // raw 24h base volume from ticker
}

/// A single price observation.
#[derive(Debug, Clone, Copy)]
struct PricePoint {
    price: Decimal,
    ts: u64, // unix millis
    volume: f64, // per-tick differenced volume
}

// ── the feed ─────────────────────────────────────────────────

pub struct BinanceFeed {
    rx: watch::Receiver<TickerUpdate>,
    history: VecDeque<PricePoint>,
    momentum_window_secs: u64,

    // EMA state for MACD
    ema_fast: f64,
    ema_slow: f64,
    ema_signal: f64,

    // RSI state (Wilder smoothing)
    avg_gain: f64,
    avg_loss: f64,
    prev_price: Option<f64>,

    // Volume state
    running_avg_volume: f64,
    last_24h_volume: f64,

    // Seeding
    indicators_seeded: bool,
}

impl BinanceFeed {
    pub async fn new(symbol: &str) -> Result<Self> {
        let (tx, rx) = watch::channel(TickerUpdate {
            price: Decimal::ZERO,
            volume: 0.0,
        });

        let stream_symbol = symbol.to_lowercase();
        let url = format!("wss://stream.binance.com:9443/ws/{stream_symbol}@ticker");

        info!(symbol = %symbol, "starting Binance feed with composite indicators");

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
            momentum_window_secs: 300,
            ema_fast: 0.0,
            ema_slow: 0.0,
            ema_signal: 0.0,
            avg_gain: 0.0,
            avg_loss: 0.0,
            prev_price: None,
            running_avg_volume: 0.0,
            last_24h_volume: 0.0,
            indicators_seeded: false,
        })
    }

    pub fn latest_price(&self) -> Decimal {
        self.rx.borrow().price
    }

    /// Returns the latest price (used by legacy callers that need the side effect).
    pub fn price(&mut self) -> Decimal {
        let update = self.rx.borrow().clone();
        if update.price > Decimal::ZERO {
            let now = now_millis();
            let per_tick_vol = self.diff_volume(update.volume);
            self.history.push_back(PricePoint {
                price: update.price,
                ts: now,
                volume: per_tick_vol,
            });
            self.trim_history();
        }
        update.price
    }

    /// Compute composite indicator signal.
    /// Returns (pct_change, signal) where signal is a normalized 0-1 probability.
    ///   0.5 = neutral, >0.5 = bullish, <0.5 = bearish
    pub fn momentum_signal(&mut self) -> (f64, f64) {
        let update = self.rx.borrow().clone();
        let p = update.price;

        if p <= Decimal::ZERO {
            return (0.0, SIGNAL_NEUTRAL);
        }

        let now = now_millis();
        let per_tick_vol = self.diff_volume(update.volume);
        self.update_volume_avg(per_tick_vol);

        self.history.push_back(PricePoint {
            price: p,
            ts: now,
            volume: per_tick_vol,
        });
        self.trim_history();

        let n = self.history.len();
        if n < 2 {
            return (0.0, SIGNAL_NEUTRAL);
        }

        // Legacy momentum pct_change (always computed for dashboard / logging)
        let oldest = self.history.front().unwrap().price;
        let current = self.history.back().unwrap().price;
        let pct_change = if oldest > Decimal::ZERO {
            ((current - oldest) / oldest).to_f64().unwrap_or(0.0)
        } else {
            0.0
        };

        // Legacy signal as fallback
        let legacy_signal = (SIGNAL_NEUTRAL + pct_change * 10.0).clamp(SIGNAL_MIN, SIGNAL_MAX);

        // Progressive degradation: enable indicators as history builds
        let price_f = p.to_f64().unwrap_or(0.0);

        let signal = match n {
            0..=14 => {
                // Not enough data for RSI — pure momentum
                legacy_signal
            }
            15..=19 => {
                // Momentum + RSI
                if !self.indicators_seeded && n == 15 {
                    self.seed_rsi();
                }
                self.update_rsi(price_f);
                let rsi_sig = self.compute_rsi_signal();
                (W_MOMENTUM * legacy_signal + W_RSI * rsi_sig) / (W_MOMENTUM + W_RSI)
            }
            20..=25 => {
                // Momentum + RSI + Bollinger
                if !self.indicators_seeded && n == 20 {
                    self.seed_rsi();
                }
                self.update_rsi(price_f);
                let rsi_sig = self.compute_rsi_signal();
                let bb_sig = self.compute_bollinger(price_f);
                (W_MOMENTUM * legacy_signal + W_RSI * rsi_sig + W_BB * bb_sig)
                    / (W_MOMENTUM + W_RSI + W_BB)
            }
            _ => {
                // Full composite
                if !self.indicators_seeded {
                    self.seed_all(price_f);
                    self.indicators_seeded = true;
                }
                self.update_all(price_f);

                let sig_mom = legacy_signal;
                let sig_rsi = self.compute_rsi_signal();
                let sig_macd = self.compute_macd_signal();
                let sig_bb = self.compute_bollinger(price_f);

                let composite_raw = W_MOMENTUM * sig_mom
                    + W_RSI * sig_rsi
                    + W_MACD * sig_macd
                    + W_BB * sig_bb;

                // Volume confidence: pull toward neutral on low volume
                let vol_conf = self.volume_confidence();
                SIGNAL_NEUTRAL + (composite_raw - SIGNAL_NEUTRAL) * vol_conf
            }
        };

        let signal = signal.clamp(SIGNAL_MIN, SIGNAL_MAX);
        (pct_change, signal)
    }

    pub fn btc_return_5s(&self) -> f64 {
        self.btc_return_ns(5)
    }

    pub fn btc_return_30s(&self) -> f64 {
        self.btc_return_ns(30)
    }

    fn btc_return_ns(&self, secs: u64) -> f64 {
        let cutoff = now_millis() - (secs * 1000);
        let oldest = self.history.iter().find(|p| p.ts >= cutoff);
        let current = self.history.back();
        match (oldest, current) {
            (Some(o), Some(c)) if o.price > Decimal::ZERO => {
                ((c.price - o.price) / o.price).to_f64().unwrap_or(0.0)
            }
            _ => 0.0,
        }
    }

    // ── volume ────────────────────────────────────────────

    fn diff_volume(&mut self, raw_24h: f64) -> f64 {
        if self.last_24h_volume == 0.0 {
            self.last_24h_volume = raw_24h;
            return 0.0;
        }
        if raw_24h < self.last_24h_volume {
            // UTC midnight reset — use new day's volume directly
            self.last_24h_volume = raw_24h;
            return raw_24h;
        }
        let diff = raw_24h - self.last_24h_volume;
        self.last_24h_volume = raw_24h;
        diff
    }

    fn update_volume_avg(&mut self, per_tick: f64) {
        if per_tick <= 0.0 {
            return;
        }
        if self.running_avg_volume == 0.0 {
            self.running_avg_volume = per_tick;
        } else {
            self.running_avg_volume =
                VOLUME_EMA_ALPHA * per_tick + (1.0 - VOLUME_EMA_ALPHA) * self.running_avg_volume;
        }
    }

    fn volume_confidence(&self) -> f64 {
        if self.running_avg_volume <= 0.0 {
            return 1.0;
        }
        let last_vol = self.history.back().map(|p| p.volume).unwrap_or(0.0);
        (last_vol / self.running_avg_volume).clamp(0.0, 1.0)
    }

    // ── EMA (MACD) ────────────────────────────────────────

    fn seed_all(&mut self, price: f64) {
        self.seed_emas();
        self.seed_rsi();
        self.prev_price = Some(price);
    }

    fn seed_emas(&mut self) {
        let n = self.history.len();
        let fast_n = MACD_FAST.min(n);
        let slow_n = MACD_SLOW.min(n);

        let prices: Vec<f64> = self
            .history
            .iter()
            .map(|pp| pp.price.to_f64().unwrap_or(0.0))
            .collect();

        self.ema_fast = prices.iter().rev().take(fast_n).sum::<f64>() / fast_n as f64;
        self.ema_slow = prices.iter().rev().take(slow_n).sum::<f64>() / slow_n as f64;
        self.ema_signal = self.ema_fast - self.ema_slow; // MACD line
    }

    fn update_all(&mut self, price: f64) {
        // EMA
        self.ema_fast = price * EMA_FAST_MULT + self.ema_fast * (1.0 - EMA_FAST_MULT);
        self.ema_slow = price * EMA_SLOW_MULT + self.ema_slow * (1.0 - EMA_SLOW_MULT);
        let macd_line = self.ema_fast - self.ema_slow;
        self.ema_signal = macd_line * EMA_SIGNAL_MULT + self.ema_signal * (1.0 - EMA_SIGNAL_MULT);

        // RSI
        self.update_rsi(price);
    }

    fn compute_macd_signal(&self) -> f64 {
        let macd_line = self.ema_fast - self.ema_slow;
        let histogram = macd_line - self.ema_signal;

        if self.ema_slow <= 0.0 {
            return SIGNAL_NEUTRAL;
        }

        // Normalize histogram as % of price for scale-invariance
        let norm = (histogram / self.ema_slow) * 1000.0; // ~bps scale
        (SIGNAL_NEUTRAL + norm.clamp(-0.4, 0.4)).clamp(SIGNAL_MIN, SIGNAL_MAX)
    }

    // ── RSI (Wilder smoothing) ────────────────────────────

    fn seed_rsi(&mut self) {
        if self.history.len() < RSI_PERIOD + 1 {
            return;
        }

        let mut sum_gain = 0.0f64;
        let mut sum_loss = 0.0f64;

        let window: Vec<f64> = self
            .history
            .iter()
            .rev()
            .take(RSI_PERIOD + 1)
            .map(|pp| pp.price.to_f64().unwrap_or(0.0))
            .collect();

        for i in 1..window.len() {
            let change = window[i - 1] - window[i]; // newer - older
            if change > 0.0 {
                sum_gain += change;
            } else {
                sum_loss += -change;
            }
        }

        self.avg_gain = sum_gain / RSI_PERIOD as f64;
        self.avg_loss = sum_loss / RSI_PERIOD as f64;
    }

    fn update_rsi(&mut self, price: f64) {
        if let Some(prev) = self.prev_price {
            let change = price - prev;
            let gain = if change > 0.0 { change } else { 0.0 };
            let loss = if change < 0.0 { -change } else { 0.0 };

            self.avg_gain = (self.avg_gain * (RSI_PERIOD - 1) as f64 + gain) / RSI_PERIOD as f64;
            self.avg_loss =
                (self.avg_loss * (RSI_PERIOD - 1) as f64 + loss) / RSI_PERIOD as f64;
        }
        self.prev_price = Some(price);
    }

    fn compute_rsi_signal(&self) -> f64 {
        let rsi = if self.avg_loss <= 0.0 {
            100.0
        } else {
            let rs = self.avg_gain / self.avg_loss;
            100.0 - 100.0 / (1.0 + rs)
        };

        // RSI > 70 = overbought (bearish), RSI < 30 = oversold (bullish)
        // Map: RSI 50 → 0.5, RSI 30 → 0.78, RSI 70 → 0.22
        (SIGNAL_NEUTRAL + (50.0 - rsi) * 0.014).clamp(SIGNAL_MIN, SIGNAL_MAX)
    }

    // ── Bollinger Bands ───────────────────────────────────

    fn compute_bollinger(&self, current_price: f64) -> f64 {
        let n = BB_PERIOD.min(self.history.len());
        if n < 2 {
            return SIGNAL_NEUTRAL;
        }

        let prices: Vec<f64> = self
            .history
            .iter()
            .rev()
            .take(n)
            .map(|pp| pp.price.to_f64().unwrap_or(0.0))
            .collect();

        let mean = prices.iter().sum::<f64>() / n as f64;
        let variance = prices.iter().map(|p| (p - mean).powi(2)).sum::<f64>() / n as f64;
        let std_dev = variance.sqrt();

        let upper = mean + BB_STDDEV * std_dev;
        let lower = mean - BB_STDDEV * std_dev;

        if (upper - lower).abs() < 1e-9 {
            return SIGNAL_NEUTRAL;
        }

        // position: 0.0 = at lower band (oversold → bullish), 1.0 = at upper band (overbought → bearish)
        let position = ((current_price - lower) / (upper - lower)).clamp(0.0, 1.0);
        let signal = 1.0 - position; // invert: low position → high signal (bullish)
        signal.clamp(SIGNAL_MIN, SIGNAL_MAX)
    }

    // ── housekeeping ──────────────────────────────────────

    fn trim_history(&mut self) {
        let cutoff = now_millis() - (self.momentum_window_secs * 1000);
        while self.history.front().map_or(false, |p| p.ts < cutoff) {
            self.history.pop_front();
        }
    }
}

// ── helpers ──────────────────────────────────────────────────

fn now_millis() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64
}

async fn run_ws_loop(url: &str, tx: &watch::Sender<TickerUpdate>) -> Result<()> {
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
                        let volume = ticker.base_volume.parse::<f64>().unwrap_or(0.0);
                        let _ = tx.send(TickerUpdate { price, volume });
                    } else {
                        warn!(last_price = %ticker.last_price, "Binance ticker price parse failed");
                    }
                } else {
                    // Log parse failures to diagnose format issues
                    let snippet = if text.len() > 100 { &text[..100] } else { &text };
                    debug!(msg = snippet, "Binance message not a ticker");
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
