use crate::models::GammaMarket;
use anyhow::{Context, Result};
use reqwest::{Client, Url};
use serde::Deserialize;
use std::collections::HashSet;

#[derive(Clone)]
pub struct GammaClient {
    base_url: Url,
    clob_url: Url,
    http: Client,
}

/// CLOB market response — the format returned by clob.polymarket.com/markets/{conditionId}
#[derive(Debug, Deserialize)]
struct ClobMarketResponse {
    #[serde(default)]
    condition_id: String,
    #[serde(default)]
    question: String,
    #[serde(default)]
    active: bool,
    #[serde(default)]
    closed: bool,
    #[serde(default)]
    accepting_orders: bool,
    #[serde(default)]
    market_slug: String,
    #[serde(default)]
    end_date_iso: Option<String>,
    #[serde(default)]
    tokens: Vec<ClobToken>,
    #[serde(default)]
    minimum_tick_size: Option<f64>,
}

#[derive(Debug, Deserialize)]
struct ClobToken {
    token_id: String,
    outcome: String,
    #[serde(default)]
    price: f64,
    #[serde(default)]
    winner: bool,
}

impl GammaClient {
    pub fn new(gamma_base_url: String) -> Result<Self> {
        Self::with_clob(gamma_base_url, "https://clob.polymarket.com".to_string())
    }

    pub fn with_clob(gamma_base_url: String, clob_base_url: String) -> Result<Self> {
        Ok(Self {
            base_url: Url::parse(&gamma_base_url).context("invalid gamma_base_url")?,
            clob_url: Url::parse(&clob_base_url).context("invalid clob_base_url")?,
            http: Client::builder().build()?,
        })
    }

    /// Fetch active, open markets — paginated to cover deeper listings.
    /// Also fetches ephemeral crypto "Up or Down" markets directly from
    /// the CLOB API, since these are hidden from the standard Gamma feed.
    pub async fn active_markets(&self, limit: usize) -> Result<Vec<GammaMarket>> {
        let mut seen_ids = HashSet::new();
        let mut all_markets = Vec::new();

        // 1. Default active markets from Gamma (general discovery)
        let general = self.fetch_page(limit, None).await?;
        for m in general {
            if seen_ids.insert(m.condition_or_id()) {
                all_markets.push(m);
            }
        }

        // 2. Targeted Gamma queries for crypto keywords
        let crypto_tags = ["bitcoin", "btc", "ethereum", "eth", "crypto", "solana"];
        for tag in &crypto_tags {
            match self.fetch_page(50, Some(tag)).await {
                Ok(results) => {
                    for m in results {
                        if seen_ids.insert(m.condition_or_id()) {
                            all_markets.push(m);
                        }
                    }
                }
                Err(e) => {
                    tracing::warn!(tag, "crypto keyword fetch failed: {}", e);
                }
            }
        }

        // 3. Fetch ephemeral "Up or Down" crypto markets from the CLOB API
        //    These use dynamic slugs like btc-updown-5m-{unix_timestamp}
        let updown_markets = self.fetch_updown_markets().await;
        let count = updown_markets.len();
        for m in updown_markets {
            if seen_ids.insert(m.condition_or_id()) {
                all_markets.push(m);
            }
        }
        if count > 0 {
            tracing::info!(count, "fetched Up/Down crypto markets from CLOB API");
        }

        Ok(all_markets)
    }

    /// Fetch the ephemeral "Up or Down" crypto markets from the CLOB API.
    ///
    /// These markets are created every 5/15 minutes with slug patterns like:
    ///   btc-updown-5m-{unix_timestamp}
    ///   eth-updown-15m-{unix_timestamp}
    ///
    /// They are NOT indexed in the Gamma API (tagged "Hide From New").
    /// We compute the current and next window timestamps and probe the CLOB.
    async fn fetch_updown_markets(&self) -> Vec<GammaMarket> {
        let assets = ["btc", "eth", "sol", "xrp", "doge", "bnb"];
        let timeframes = [
            ("5m", 300u64),   // 5 minutes
            ("15m", 900u64),  // 15 minutes
        ];

        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs();

        let mut markets = Vec::new();

        for asset in &assets {
            for (label, interval) in &timeframes {
                // Compute the current window's start timestamp (aligned to interval)
                let current_start = (now / interval) * interval;
                // Also try the previous window (may still be accepting orders briefly)
                let prev_start = current_start.saturating_sub(*interval);
                // And the next window (may already be created)
                let next_start = current_start + interval;

                for ts in [prev_start, current_start, next_start] {
                    let slug = format!("{}-updown-{}-{}", asset, label, ts);
                    match self.fetch_clob_market_by_slug(&slug).await {
                        Ok(Some(m)) => {
                            // Only include if it's active and accepting orders
                            if m.active == Some(true) && m.closed != Some(true) {
                                tracing::debug!(
                                    slug = %slug,
                                    question = %m.question,
                                    "discovered Up/Down market"
                                );
                                markets.push(m);
                            }
                        }
                        Ok(None) => {} // market not found or not tradable
                        Err(e) => {
                            tracing::trace!(slug = %slug, "CLOB probe failed: {}", e);
                        }
                    }
                }
            }
        }

        markets
    }

    /// Fetch a single market from the CLOB API by its slug (used as condition_id lookup).
    /// Returns None if the market doesn't exist (404) or isn't tradable.
    async fn fetch_clob_market_by_slug(&self, slug: &str) -> Result<Option<GammaMarket>> {
        // The CLOB API doesn't have a slug-based lookup, but we can use the
        // Gamma /markets endpoint with an exact slug match
        let mut url = self.base_url.join("/markets")?;
        {
            let mut q = url.query_pairs_mut();
            q.append_pair("slug", slug)
                .append_pair("limit", "1");
        }

        let response = self.http.get(url).send().await?;

        if !response.status().is_success() {
            return Ok(None);
        }

        let markets = response
            .json::<Vec<GammaMarket>>()
            .await
            .unwrap_or_default();

        if let Some(m) = markets.into_iter().next() {
            // Verify it's actually an Up/Down market
            if m.question.to_lowercase().contains("up or down") {
                return Ok(Some(m));
            }
        }

        // Fallback: try CLOB API directly with the slug as a condition_id search
        // The CLOB API format is: /markets?market_slug={slug}
        let clob_url = format!("{}markets?market_slug={}", self.clob_url, slug);
        match self.http.get(&clob_url).send().await {
            Ok(resp) if resp.status().is_success() => {
                match resp.json::<Vec<ClobMarketResponse>>().await {
                    Ok(clob_markets) => {
                        for cm in clob_markets {
                            if cm.active && !cm.closed && cm.accepting_orders {
                                return Ok(Some(clob_to_gamma(cm)));
                            }
                        }
                    }
                    // Single object response
                    Err(_) => {}
                }
            }
            _ => {}
        }

        Ok(None)
    }

    async fn fetch_page(
        &self,
        limit: usize,
        slug_keyword: Option<&str>,
    ) -> Result<Vec<GammaMarket>> {
        let mut url = self.base_url.join("/markets")?;
        {
            let mut q = url.query_pairs_mut();
            q.append_pair("active", "true")
                .append_pair("closed", "false")
                .append_pair("limit", &limit.to_string());
            if let Some(kw) = slug_keyword {
                q.append_pair("slug_contains", kw);
            }
        }

        let response = self
            .http
            .get(url)
            .send()
            .await?
            .error_for_status()
            .context("Gamma /markets request failed")?;

        let markets = response
            .json::<Vec<GammaMarket>>()
            .await
            .context("failed to decode Gamma /markets response")?;

        Ok(markets)
    }
}

/// Convert a CLOB market response into a GammaMarket struct.
fn clob_to_gamma(cm: ClobMarketResponse) -> GammaMarket {
    let outcomes: Vec<String> = cm.tokens.iter().map(|t| t.outcome.clone()).collect();
    let prices: Vec<String> = cm.tokens.iter().map(|t| format!("{}", t.price)).collect();
    let token_ids: Vec<String> = cm.tokens.iter().map(|t| t.token_id.clone()).collect();

    GammaMarket {
        id: cm.market_slug.clone(),
        condition_id: Some(cm.condition_id),
        question: cm.question,
        active: Some(cm.active),
        closed: Some(cm.closed),
        end_date: cm.end_date_iso,
        outcome_prices: if prices.is_empty() { None } else { Some(prices) },
        clob_token_ids: if token_ids.is_empty() { None } else { Some(token_ids) },
        outcomes: if outcomes.is_empty() { None } else { Some(outcomes) },
        volume: None,
        liquidity: None,
        extra: std::collections::HashMap::new(),
    }
}
