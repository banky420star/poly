use anyhow::{Context, Result};
use futures_util::{SinkExt, StreamExt};
use serde::Deserialize;
use std::collections::HashMap;
use tokio::sync::watch;
use tokio_tungstenite::{connect_async, tungstenite::Message};
use tracing::{debug, info, warn};


#[derive(Debug, Clone, Deserialize)]
pub struct WsBookLevel {
    pub price: String,
    pub size: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct WsBookEvent {
    #[serde(rename = "asset_id")]
    pub asset_id: String,
    #[serde(default)]
    pub bids: Vec<WsBookLevel>,
    #[serde(default)]
    pub asks: Vec<WsBookLevel>,
    #[serde(default, rename = "last_trade_price")]
    pub last_trade_price: Option<String>,
    #[serde(default, rename = "tick_size")]
    pub tick_size: Option<String>,
    #[serde(default, rename = "hash")]
    pub hash: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct WsPriceChangeEvent {
    #[serde(rename = "asset_id")]
    pub asset_id: String,
    pub price: String,
    #[serde(default)]
    pub side: Option<String>,
    #[serde(default)]
    pub size: Option<String>,
    #[serde(default)]
    pub timestamp: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct WsBestBidAsk {
    #[serde(rename = "asset_id")]
    pub asset_id: String,
    pub best_bid: Option<String>,
    pub best_ask: Option<String>,
}

#[derive(Debug, Clone)]
pub struct MarketSnapshot {
    pub asset_id: String,
    pub best_bid: Option<rust_decimal::Decimal>,
    pub best_ask: Option<rust_decimal::Decimal>,
    pub mid_price: Option<rust_decimal::Decimal>,
    pub last_trade_price: Option<rust_decimal::Decimal>,
}

#[derive(Debug, Clone, Default)]
pub struct MarketBookCache {
    books: HashMap<String, MarketSnapshot>,
}

impl MarketBookCache {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn update_book(&mut self, event: &WsBookEvent) {
        use rust_decimal::prelude::FromStr;
        use rust_decimal::Decimal;

        let best_bid = event.bids.first().and_then(|l| Decimal::from_str(&l.price).ok());
        let best_ask = event.asks.first().and_then(|l| Decimal::from_str(&l.price).ok());
        let mid_price = match (best_bid, best_ask) {
            (Some(bid), Some(ask)) => Some((bid + ask) / Decimal::TWO),
            _ => None,
        };
        let last_trade_price = event
            .last_trade_price
            .as_ref()
            .and_then(|p| Decimal::from_str(p).ok());

        self.books.insert(
            event.asset_id.clone(),
            MarketSnapshot {
                asset_id: event.asset_id.clone(),
                best_bid,
                best_ask,
                mid_price,
                last_trade_price,
            },
        );
    }

    pub fn update_price_change(&mut self, event: &WsPriceChangeEvent) {
        use rust_decimal::prelude::FromStr;
        use rust_decimal::Decimal;

        let entry = self
            .books
            .entry(event.asset_id.clone())
            .or_insert(MarketSnapshot {
                asset_id: event.asset_id.clone(),
                best_bid: None,
                best_ask: None,
                mid_price: None,
                last_trade_price: None,
            });

        if let Ok(price) = Decimal::from_str(&event.price) {
            entry.last_trade_price = Some(price);
            // Update bid/ask based on side
            match event.side.as_deref() {
                Some("BUY") => entry.best_bid = Some(price),
                Some("SELL") => entry.best_ask = Some(price),
                _ => {}
            }
            entry.mid_price = match (entry.best_bid, entry.best_ask) {
                (Some(bid), Some(ask)) => Some((bid + ask) / Decimal::TWO),
                _ => None,
            };
        }
    }

    pub fn update_best_bid_ask(&mut self, event: &WsBestBidAsk) {
        use rust_decimal::prelude::FromStr;
        use rust_decimal::Decimal;

        let entry = self
            .books
            .entry(event.asset_id.clone())
            .or_insert(MarketSnapshot {
                asset_id: event.asset_id.clone(),
                best_bid: None,
                best_ask: None,
                mid_price: None,
                last_trade_price: None,
            });

        entry.best_bid = event.best_bid.as_ref().and_then(|p| Decimal::from_str(p).ok());
        entry.best_ask = event.best_ask.as_ref().and_then(|p| Decimal::from_str(p).ok());
        entry.mid_price = match (entry.best_bid, entry.best_ask) {
            (Some(bid), Some(ask)) => Some((bid + ask) / Decimal::TWO),
            _ => None,
        };
    }

    pub fn get(&self, asset_id: &str) -> Option<&MarketSnapshot> {
        self.books.get(asset_id)
    }

    pub fn all_snapshots(&self) -> &HashMap<String, MarketSnapshot> {
        &self.books
    }
}

pub struct PolyWsFeed {
    latest: watch::Receiver<MarketBookCache>,
}

impl PolyWsFeed {
    pub async fn new(token_ids: Vec<String>) -> Result<Self> {
        let (tx, rx) = watch::channel(MarketBookCache::new());

        let url = "wss://ws-subscriptions-clob.polymarket.com/ws/market";
        let token_ids_clone = token_ids.clone();

        tokio::spawn(async move {
            loop {
                if let Err(e) = run_ws_loop(url, &token_ids_clone, &tx).await {
                    tracing::error!(error = %e, "Polymarket WS error, reconnecting in 5s");
                }
                tokio::time::sleep(std::time::Duration::from_secs(5)).await;
                info!("reconnecting Polymarket WS");
            }
        });

        Ok(Self { latest: rx })
    }

    pub fn snapshot(&self) -> MarketBookCache {
        self.latest.borrow().clone()
    }

    pub fn subscribe(&self) -> watch::Receiver<MarketBookCache> {
        self.latest.clone()
    }
}

async fn run_ws_loop(
    url: &str,
    token_ids: &[String],
    tx: &watch::Sender<MarketBookCache>,
) -> Result<()> {
    let (ws_stream, _) = connect_async(url)
        .await
        .context("failed to connect to Polymarket WS")?;

    info!("Polymarket WS connected");

    let (mut write, mut read) = ws_stream.split();

    let subscribe_msg = serde_json::json!({
        "type": "market",
        "assets_ids": token_ids,
        "initial_dump": true,
        "level": 2,
        "custom_feature_enabled": true,
    });

    write
        .send(Message::Text(subscribe_msg.to_string()))
        .await
        .context("failed to send subscribe message")?;

    info!(tokens = token_ids.len(), "subscribed to Polymarket market channel");

    // PING task
    let mut ping_write = write;
    tokio::spawn(async move {
        let mut interval = tokio::time::interval(std::time::Duration::from_secs(10));
        loop {
            interval.tick().await;
            if ping_write.send(Message::Ping(vec![])).await.is_err() {
                break;
            }
        }
    });

    while let Some(msg) = read.next().await {
        match msg {
            Ok(Message::Text(text)) => {
                if text == "PONG" {
                    continue;
                }

                let mut cache = tx.borrow().clone();

                if let Ok(event) = serde_json::from_str::<serde_json::Value>(&text) {
                    let event_type = event
                        .get("event_type")
                        .and_then(|v| v.as_str())
                        .unwrap_or("");

                    match event_type {
                        "book" => {
                            if let Ok(book_event) =
                                serde_json::from_value::<WsBookEvent>(event)
                            {
                                debug!(asset_id = %book_event.asset_id, "book update");
                                cache.update_book(&book_event);
                            }
                        }
                        "price_change" => {
                            if let Ok(price_event) =
                                serde_json::from_value::<WsPriceChangeEvent>(event)
                            {
                                debug!(asset_id = %price_event.asset_id, "price change");
                                cache.update_price_change(&price_event);
                            }
                        }
                        "best_bid_ask" => {
                            if let Ok(bba_event) =
                                serde_json::from_value::<WsBestBidAsk>(event)
                            {
                                debug!(asset_id = %bba_event.asset_id, "best bid/ask");
                                cache.update_best_bid_ask(&bba_event);
                            }
                        }
                        _ => {
                            debug!(event_type = %event_type, "unhandled WS event");
                        }
                    }
                }

                let _ = tx.send(cache);
            }
            Ok(Message::Close(_)) => {
                warn!("Polymarket WS closed by server");
                break;
            }
            Err(e) => {
                tracing::error!(error = %e, "Polymarket WS read error");
                break;
            }
            _ => {}
        }
    }

    Ok(())
}