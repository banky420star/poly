use crate::{
    config::{FeesConfig, StrategyConfig},
    models::{DepthData, GammaMarket},
    paper::{PaperLedger, QuoteIntent},
    poly_ws::MarketSnapshot,
};
use anyhow::Result;
use chrono::{DateTime, Utc};
use rust_decimal::prelude::ToPrimitive;
use rust_decimal::Decimal;
use rust_decimal_macros::dec;
use std::collections::HashMap;

pub struct ReactiveDirectional {
    cfg: StrategyConfig,
    fees: FeesConfig,
}

impl ReactiveDirectional {
    pub fn new(cfg: StrategyConfig, fees: FeesConfig) -> Self {
        Self { cfg, fees }
    }

    pub fn build_quotes(
        &self,
        market: &GammaMarket,
        ledger: &PaperLedger,
        momentum_signal: f64,
        time_left: Option<u64>,
        book_snapshots: Option<&HashMap<String, MarketSnapshot>>,
        depth: Option<&DepthData>,
    ) -> Result<Vec<QuoteIntent>> {
        let mut quotes = Vec::new();

        let Some(yes_mid) = market.yes_mid() else {
            return Ok(quotes);
        };

        if yes_mid < self.cfg.min_yes_mid || yes_mid > self.cfg.max_yes_mid {
            return Ok(quotes);
        }

        let no_mid = market.no_mid().unwrap_or(Decimal::ONE - yes_mid);

        // Use real order book data if available, otherwise fall back to mid
        let (best_bid, best_ask, real_spread_bps) =
            extract_book_data(market, book_snapshots, yes_mid);

        // Fee awareness
        let is_maker = should_quote_as_maker(&self.fees, time_left);

        let window_secs = self.cfg.directional_load_window_seconds;
        let in_load_window = time_left.map_or(false, |t| t <= window_secs);

        let directional_skew = compute_skew(momentum_signal, yes_mid);
        let skew_strength = directional_skew.abs();

        // Dynamic position sizing: scale load size with skew confidence
        let base_size = ledger.default_order_size_usdc();
        let load_size = if skew_strength >= self.cfg.min_skew_ratio * 2.0 {
            (base_size * dec!(8)).min(dec!(100))
        } else if skew_strength >= self.cfg.min_skew_ratio * 1.5 {
            (base_size * dec!(6)).min(dec!(80))
        } else {
            (base_size * dec!(4)).min(dec!(60))
        };

        // Depth-aware sizing: scale down if order book is thin
        let depth_multiplier = depth.map_or(dec!(1), |d| {
            let ask_depth = if directional_skew > 0.0 {
                d.yes_ask_depth_usdc
            } else {
                d.no_ask_depth_usdc
            };
            // Full size if depth >= 3x our order, proportional otherwise
            if ask_depth <= Decimal::ZERO {
                dec!(0) // No depth = no trade
            } else {
                let ratio = ask_depth / (load_size * dec!(3));
                ratio.min(dec!(1))
            }
        });

        let effective_load_size = (load_size * depth_multiplier).round_dp(2);
        let effective_base_size = (base_size * depth_multiplier).round_dp(2);

        if in_load_window && skew_strength >= self.cfg.min_skew_ratio && effective_load_size > Decimal::ZERO {
            // Directional load: we're takers, account for taker fee
            if directional_skew > 0.0 && self.cfg.allow_buy_yes {
                if let Some(token_id) = market.yes_token_id() {
                    let max_price = adjust_price_for_fees(
                        yes_mid + dec!(0.01),
                        false, // taker
                        &self.fees,
                        self.cfg.maker_edge_bps,
                    );
                    let price = clamp_probability(max_price).min(best_ask);
                    quotes.push(QuoteIntent::buy(
                        market.id.clone(),
                        market.condition_or_id(),
                        token_id,
                        "YES".to_string(),
                        price,
                        effective_load_size,
                        format!(
                            "directional_load YES skew={:.2} spread={}bps fees={}bps depth_mult={:.2} time_left={:?}",
                            directional_skew,
                            real_spread_bps.unwrap_or(0),
                            self.fees.taker_fee_bps,
                            depth_multiplier,
                            time_left
                        ),
                    ));
                }
            } else if directional_skew < 0.0 && self.cfg.allow_buy_no {
                if let Some(token_id) = market.no_token_id() {
                    let max_price = adjust_price_for_fees(
                        no_mid + dec!(0.01),
                        false,
                        &self.fees,
                        self.cfg.maker_edge_bps,
                    );
                    let price = clamp_probability(max_price).min(best_ask);
                    quotes.push(QuoteIntent::buy(
                        market.id.clone(),
                        market.condition_or_id(),
                        token_id,
                        "NO".to_string(),
                        price,
                        effective_load_size,
                        format!(
                            "directional_load NO skew={:.2} spread={}bps fees={}bps depth_mult={:.2} time_left={:?}",
                            directional_skew,
                            real_spread_bps.unwrap_or(0),
                            self.fees.taker_fee_bps,
                            depth_multiplier,
                            time_left
                        ),
                    ));
                }
            }
        }

        // Market-making quotes: suppress during strong directional load
        if !(in_load_window && skew_strength >= self.cfg.min_skew_ratio) && effective_base_size > Decimal::ZERO {
            if real_spread_bps.map_or(true, |s| s < self.cfg.quote_spread_bps * 3) {
                if self.cfg.allow_buy_yes {
                    if let Some(token_id) = market.yes_token_id() {
                        let price = adjust_price_for_fees(
                            best_bid,
                            is_maker,
                            &self.fees,
                            self.cfg.maker_edge_bps,
                        );
                        let price = clamp_probability(price);
                        if price > dec!(0.01) {
                            quotes.push(QuoteIntent::buy(
                                market.id.clone(),
                                market.condition_or_id(),
                                token_id,
                                "YES".to_string(),
                                price,
                                effective_base_size,
                                format!(
                                    "maker_quote YES bid={} mid={} spread={}bps fees={}bps depth_mult={:.2}",
                                    best_bid, yes_mid,
                                    real_spread_bps.unwrap_or(0),
                                    self.fees.maker_fee_bps,
                                    depth_multiplier,
                                ),
                            ));
                        }
                    }
                }

                if self.cfg.allow_buy_no {
                    if let Some(token_id) = market.no_token_id() {
                        let no_best_bid = Decimal::ONE - best_ask;
                        let price = adjust_price_for_fees(
                            no_best_bid,
                            is_maker,
                            &self.fees,
                            self.cfg.maker_edge_bps,
                        );
                        let price = clamp_probability(price);
                        if price > dec!(0.01) {
                            quotes.push(QuoteIntent::buy(
                                market.id.clone(),
                                market.condition_or_id(),
                                token_id,
                                "NO".to_string(),
                                price,
                                effective_base_size,
                                format!(
                                    "maker_quote NO bid={} mid={} spread={}bps fees={}bps depth_mult={:.2}",
                                    no_best_bid, no_mid,
                                    real_spread_bps.unwrap_or(0),
                                    self.fees.maker_fee_bps,
                                    depth_multiplier,
                                ),
                            ));
                        }
                    }
                }
            }
        }

        Ok(quotes)
    }
}

/// Adjusts price to account for fees and desired edge.
fn adjust_price_for_fees(
    price: Decimal,
    is_maker: bool,
    fees: &FeesConfig,
    desired_edge_bps: i64,
) -> Decimal {
    let fee_bps = if is_maker {
        fees.maker_fee_bps
    } else {
        fees.taker_fee_bps
    };
    let fee_decimal = bps_to_decimal(fee_bps);
    let edge_decimal = bps_to_decimal(desired_edge_bps);
    price - fee_decimal - edge_decimal
}

/// Decide if we should quote as maker based on config and time left.
fn should_quote_as_maker(fees: &FeesConfig, time_left: Option<u64>) -> bool {
    if !fees.prefer_maker {
        return false;
    }
    time_left.map_or(true, |t| t > 60)
}

/// Extract real book data from WS snapshots.
/// Returns (best_bid, best_ask, spread_bps) — falls back to mid if no book data.
fn extract_book_data(
    market: &GammaMarket,
    snapshots: Option<&HashMap<String, MarketSnapshot>>,
    yes_mid: Decimal,
) -> (Decimal, Decimal, Option<i64>) {
    let yes_token = market.yes_token_id();

    if let (Some(snaps), Some(token_id)) = (snapshots, &yes_token) {
        if let Some(snap) = snaps.get(token_id) {
            if let (Some(bid), Some(ask)) = (snap.best_bid, snap.best_ask) {
                let spread_bps = if !ask.is_zero() {
                    let spread = ask - bid;
                    let bps = (spread / ask) * Decimal::from(10000);
                    bps.to_i64()
                } else {
                    None
                };
                return (bid, ask, spread_bps);
            }
        }
    }

    // Fallback: derive from Gamma mid price with default spread
    let half_spread = dec!(0.015); // 1.5% default half spread
    (yes_mid - half_spread, yes_mid + half_spread, None)
}

/// Compute directional skew from momentum signal and market mid price.
/// Works symmetrically for both bullish and bearish directions.
///
/// `signal` is a normalized 0-1 probability from Binance price momentum:
///   0.5 = neutral, >0.5 = bullish (BTC going up), <0.5 = bearish (BTC going down)
/// `yes_mid` is the market's current YES probability.
/// `ref_price` is not needed — skew is computed purely from momentum vs market mid.
///
/// Returns skew: positive = YES is underpriced (bullish), negative = NO is underpriced (bearish).
/// Example: signal=0.70, yes_mid=0.45 → base_skew=1.0, mispricing=0.75 → skew=1.75
/// Example: signal=0.30, yes_mid=0.70 → base_skew=-1.0, mispricing=-1.20 → skew=-2.20
fn compute_skew(signal: f64, yes_mid: Decimal) -> f64 {
    let mid_f = yes_mid.to_f64().unwrap_or(0.5);
    if mid_f <= 0.01 || mid_f >= 0.99 {
        return 0.0;
    }

    // Base skew from momentum (narrow dead zone for 5m crypto scalping)
    // Dead zone: 0.49–0.51 (±1% around neutral)
    // For 5m binary markets, even a 0.02% BTC move is a meaningful signal
    let base_skew = if signal > 0.51 {
        (signal - 0.5) * 20.0     // Bullish: amplified for micro-moves
    } else if signal < 0.49 {
        (signal - 0.5) * 20.0     // Bearish: amplified for micro-moves
    } else {
        0.0                       // Dead zone: truly neutral
    };

    // Amplify when market mid disagrees with momentum direction
    let mispricing = (signal - mid_f) * 5.0;

    (base_skew + mispricing).clamp(-4.0, 4.0)
}

fn bps_to_decimal(bps: i64) -> Decimal {
    Decimal::from(bps) / dec!(10000)
}

fn clamp_probability(p: Decimal) -> Decimal {
    if p < dec!(0.01) {
        dec!(0.01)
    } else if p > dec!(0.99) {
        dec!(0.99)
    } else {
        p.round_dp(2)
    }
}

/// Parse the market end_date to compute seconds remaining.
/// Returns None if parsing fails or no end_date is set.
pub fn time_left_seconds(market: &GammaMarket) -> Option<u64> {
    let end_str = market.end_date.as_ref()?;
    let end_time = DateTime::parse_from_rfc3339(end_str).ok()?;
    let now = Utc::now();
    let diff = end_time.with_timezone(&Utc) - now;
    if diff.num_seconds() > 0 {
        Some(diff.num_seconds() as u64)
    } else {
        Some(0)
    }
}