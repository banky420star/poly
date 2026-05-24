use crate::models::GammaMarket;
use anyhow::{Context, Result};
use reqwest::Client;
use rust_decimal::Decimal;
use rust_decimal::prelude::{FromStr, ToPrimitive};
use serde::Deserialize;
use std::collections::HashMap;
use url::Url;

#[derive(Debug, Clone, Deserialize)]
pub struct BookLevel {
    pub price: String,
    pub size: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct OrderBookSummary {
    #[serde(default)]
    pub market: String,
    #[serde(default, rename = "asset_id")]
    pub asset_id: String,
    #[serde(default)]
    pub bids: Vec<BookLevel>,
    #[serde(default)]
    pub asks: Vec<BookLevel>,
    #[serde(default, rename = "last_trade_price")]
    pub last_trade_price: Option<String>,
    #[serde(default, rename = "tick_size")]
    pub tick_size: Option<String>,
    #[serde(default, rename = "min_order_size")]
    pub min_order_size: Option<String>,
    #[serde(default, rename = "neg_risk")]
    pub neg_risk: Option<bool>,
    #[serde(default, rename = "hash")]
    pub hash: Option<String>,
}

#[derive(Debug, Clone)]
pub struct SpreadInfo {
    pub best_bid: Option<Decimal>,
    pub best_ask: Option<Decimal>,
    pub spread_bps: Option<i64>,
    pub mid_price: Option<Decimal>,
    pub depth_bid_usdc: Decimal,
    pub depth_ask_usdc: Decimal,
}

impl OrderBookSummary {
    pub fn spread_info(&self) -> SpreadInfo {
        let best_bid = self.bids.first().and_then(|l| Decimal::from_str(&l.price).ok());
        let best_ask = self.asks.first().and_then(|l| Decimal::from_str(&l.price).ok());

        let mid_price = match (best_bid, best_ask) {
            (Some(bid), Some(ask)) => Some((bid + ask) / Decimal::TWO),
            _ => None,
        };

        let spread_bps = match (best_bid, best_ask) {
            (Some(bid), Some(ask)) if !ask.is_zero() => {
                let spread = ask - bid;
                let bps = (spread / ask) * Decimal::from(10000);
                bps.to_i64()
            }
            _ => None,
        };

        let depth_bid_usdc = self.bids.iter().fold(Decimal::ZERO, |acc, l| {
            let price = Decimal::from_str(&l.price).unwrap_or(Decimal::ZERO);
            let size = Decimal::from_str(&l.size).unwrap_or(Decimal::ZERO);
            acc + price * size
        });

        let depth_ask_usdc = self.asks.iter().fold(Decimal::ZERO, |acc, l| {
            let price = Decimal::from_str(&l.price).unwrap_or(Decimal::ZERO);
            let size = Decimal::from_str(&l.size).unwrap_or(Decimal::ZERO);
            acc + (price * size)
        });

        SpreadInfo {
            best_bid,
            best_ask,
            spread_bps,
            mid_price,
            depth_bid_usdc,
            depth_ask_usdc,
        }
    }
}

pub struct ClobClient {
    base_url: Url,
    http: Client,
}

impl ClobClient {
    pub fn new(base_url: String) -> Result<Self> {
        Ok(Self {
            base_url: Url::parse(&base_url).context("invalid clob_base_url")?,
            http: Client::builder()
                .timeout(std::time::Duration::from_secs(10))
                .connect_timeout(std::time::Duration::from_secs(5))
                .build()?,
        })
    }

    pub async fn get_orderbook(&self, token_id: &str) -> Result<OrderBookSummary> {
        let mut url = self.base_url.join("/book")?;
        url.query_pairs_mut()
            .append_pair("token_id", token_id);

        let resp = self
            .http
            .get(url)
            .send()
            .await?
            .error_for_status()
            .context("CLOB /book request failed")?;

        resp.json::<OrderBookSummary>()
            .await
            .context("failed to decode CLOB /book response")
    }

    pub async fn get_orderbooks_for_market(
        &self,
        market: &GammaMarket,
    ) -> Result<HashMap<String, OrderBookSummary>> {
        let mut books = HashMap::new();

        if let Some(token_ids) = &market.clob_token_ids {
            for token_id in token_ids {
                match self.get_orderbook(token_id).await {
                    Ok(book) => {
                        books.insert(token_id.clone(), book);
                    }
                    Err(e) => {
                        tracing::warn!(
                            market_id = %market.id,
                            token_id = %token_id,
                            error = %e,
                            "failed to fetch order book"
                        );
                    }
                }
            }
        }

        Ok(books)
    }

    pub async fn get_midpoint(&self, token_id: &str) -> Result<Decimal> {
        let mut url = self.base_url.join("/midpoint")?;
        url.query_pairs_mut()
            .append_pair("token_id", token_id);

        let resp = self
            .http
            .get(url)
            .send()
            .await?
            .error_for_status()
            .context("CLOB /midpoint request failed")?;

        #[derive(Deserialize)]
        struct MidpointResponse {
            mid: Option<String>,
        }

        let mid_resp = resp.json::<MidpointResponse>().await?;
        mid_resp
            .mid
            .and_then(|m| Decimal::from_str(&m).ok())
            .context("no midpoint in response")
    }
}