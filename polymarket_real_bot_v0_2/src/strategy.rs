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
        ai_signal: Option<&crate::models::AiSignal>,
        shadow_mode: bool,
    ) -> Result<Vec<QuoteIntent>> {
        let mut quotes = Vec::new();

        let Some(yes_mid) = market.yes_mid() else {
            return Ok(quotes);
        };

        if yes_mid < self.cfg.min_yes_mid || yes_mid > self.cfg.max_yes_mid {
            return Ok(quotes);
        }

        let no_mid = market.no_mid().unwrap_or(Decimal::ONE - yes_mid);

        // 1. AI Signal Override Check
        if let Some(ai) = ai_signal {
            let is_match = market.yes_token_id().as_ref() == Some(&ai.token_id)
                || market.no_token_id().as_ref() == Some(&ai.token_id);
            if is_match {
                let log_msg = format!("{} {} @ ${} size=${}", ai.action, ai.outcome, ai.limit_price, ai.shares * ai.limit_price);
                if shadow_mode {
                    tracing::info!("SHADOW MODE blocked AI Signal: {}", log_msg);
                    // Continue to fallback momentum logic
                } else {
                    tracing::info!("AI SIGNAL OVERRIDE: {}", log_msg);
                    let side = ai.action.to_uppercase();
                    let intent = if side == "SELL" {
                        QuoteIntent::sell(
                            market.id.clone(),
                            market.condition_or_id(),
                            ai.token_id.clone(),
                            ai.outcome.clone(),
                            ai.limit_price,
                            ai.shares * ai.limit_price,
                            format!("AI: {}", ai.reason),
                        )
                    } else {
                        QuoteIntent::buy(
                            market.id.clone(),
                            market.condition_or_id(),
                            ai.token_id.clone(),
                            ai.outcome.clone(),
                            ai.limit_price,
                            ai.shares * ai.limit_price,
                            format!("AI: {}", ai.reason),
                        )
                    };
                    quotes.push(intent);
                    return Ok(quotes); // Skip momentum logic
                }
            }
        }

        // Use real order book data if available, otherwise fall back to mid
        let (best_bid, best_ask, real_spread_bps) =
            extract_book_data(market, book_snapshots, yes_mid);

        // Fee awareness
        let is_maker = should_quote_as_maker(&self.fees, time_left);

        let window_secs = self.cfg.directional_load_window_seconds;
        let in_load_window = time_left.map_or(false, |t| t <= window_secs);

        let directional_skew = if self.cfg.use_orderbook_imbalance {
            compute_combined_skew(
                momentum_signal,
                yes_mid,
                depth.map_or(Decimal::ZERO, |d| d.yes_bid_depth_usdc),
                depth.map_or(Decimal::ZERO, |d| d.no_bid_depth_usdc),
                self.cfg.momentum_weight,
                self.cfg.imbalance_weight,
            )
        } else {
            compute_skew(momentum_signal, yes_mid)
        };
        let skew_strength = directional_skew.abs();

        let min_skew = if self.cfg.use_orderbook_imbalance {
            self.cfg.min_combined_skew
        } else {
            self.cfg.min_skew_ratio * 2.0
        };

        // Dynamic position sizing: scale load size with skew confidence.
        // Multipliers and caps are configurable in config.toml [strategy].
        let base_size = ledger.default_order_size_usdc();
        let load_size = if skew_strength >= min_skew {
            (base_size * self.cfg.load_multiplier_strong).min(self.cfg.load_cap_usdc)
        } else if skew_strength >= min_skew * 0.75 {
            (base_size * self.cfg.load_multiplier_medium).min(self.cfg.load_cap_usdc * dec!(0.8))
        } else {
            (base_size * self.cfg.load_multiplier_base).min(self.cfg.load_cap_usdc * dec!(0.6))
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

        // Guard: reject if spread is insane (>50% = 5000 bps means dead book)
        let max_allowed_spread = (self.cfg.quote_spread_bps * 20).max(5000);
        if real_spread_bps.unwrap_or(0) > max_allowed_spread {
            tracing::debug!(
                "spread too wide for load: {} bps > {} bps max",
                real_spread_bps.unwrap_or(0),
                max_allowed_spread
            );
            return Ok(quotes);
        }

        if in_load_window && skew_strength >= min_skew && effective_load_size > Decimal::ZERO {
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
                            "directional_load YES skew={:.2} spread={}bps fees={} depth_mult={:.2} time_left={:?}",
                            directional_skew,
                            real_spread_bps.unwrap_or(0),
                            self.fees.crypto_taker_fee_rate,
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
                            "directional_load NO skew={:.2} spread={}bps fees={} depth_mult={:.2} time_left={:?}",
                            directional_skew,
                            real_spread_bps.unwrap_or(0),
                            self.fees.crypto_taker_fee_rate,
                            depth_multiplier,
                            time_left
                        ),
                    ));
                }
            }
        }

        // Market-making quotes: suppress during strong directional load
        if !(in_load_window && skew_strength >= min_skew) && effective_base_size > Decimal::ZERO {
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
                                    "maker_quote YES bid={} mid={} spread={}bps fees={} depth_mult={:.2}",
                                    best_bid, yes_mid,
                                    real_spread_bps.unwrap_or(0),
                                    self.fees.maker_fee_rate,
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
                                    "maker_quote NO bid={} mid={} spread={}bps fees={} depth_mult={:.2}",
                                    no_best_bid, no_mid,
                                    real_spread_bps.unwrap_or(0),
                                    self.fees.maker_fee_rate,
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
/// Uses the crypto taker fee rate directly from config (e.g. 0.07 for crypto).
fn adjust_price_for_fees(
    price: Decimal,
    is_maker: bool,
    fees: &FeesConfig,
    desired_edge_bps: i64,
) -> Decimal {
    let fee_rate = if is_maker {
        fees.maker_fee_rate
    } else {
        fees.crypto_taker_fee_rate
    };
    let edge_decimal = bps_to_decimal(desired_edge_bps);
    price - fee_rate - edge_decimal
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

/// Compute directional skew — FADE the indicator to exploit market-maker rebalancing.
///
/// When retail piles into one side of a binary market (e.g. BTC drops → everyone buys NO),
/// market makers accumulate a lopsided book and must push the price the OTHER way to
/// attract opposing orders and rebalance. This creates a temporary dislocat‌ion.
///
/// Strategy: fade the indicator.
///   - BTC ↑ (bullish signal) → retail buys YES → MMs push down → BUY NO
///   - BTC ↓ (bearish signal) → retail buys NO → MMs push up → BUY YES
///
/// `signal` is 0-1 from Binance composite: >0.5 bullish, <0.5 bearish.
/// Returns skew: positive = BUY YES (fading bearish), negative = BUY NO (fading bullish).
fn compute_skew(signal: f64, yes_mid: Decimal) -> f64 {
    let mid_f = yes_mid.to_f64().unwrap_or(0.5);
    if mid_f <= 0.01 || mid_f >= 0.99 {
        return 0.0;
    }

    // Fade the indicator — invert the base direction
    // Signal > 0.51 (bullish) → negative skew → BUY NO (fade the rally)
    // Signal < 0.49 (bearish) → positive skew → BUY YES (fade the dip)
    let base_skew = if signal > 0.51 {
        -(signal - 0.5) * 20.0    // Bullish → fade → BUY NO
    } else if signal < 0.49 {
        -(signal - 0.5) * 20.0    // Bearish → fade → BUY YES
    } else {
        0.0
    };

    // Mispricing: amplify fade when market mid is lagging behind the indicator.
    // If BTC is up but YES is still cheap, that's an even stronger BUY NO signal
    // (MMs haven't pushed YES down yet = bigger dislocat‌ion to exploit).
    let mispricing = (mid_f - signal) * 5.0;

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

/// Calculate Order Book Imbalance (-1.0 = heavy NO pressure, +1.0 = heavy YES pressure)
pub fn calculate_orderbook_imbalance(
    yes_bid_depth: Decimal,
    no_bid_depth: Decimal,
) -> Decimal {
    let total = yes_bid_depth + no_bid_depth;
    if total.is_zero() {
        return dec!(0);
    }
    (yes_bid_depth - no_bid_depth) / total
}

/// Combined skew = momentum (60%) + imbalance (40%)
pub fn compute_combined_skew(
    momentum_signal: f64,
    yes_mid: Decimal,
    yes_bid_depth: Decimal,
    no_bid_depth: Decimal,
    momentum_weight: f64,
    imbalance_weight: f64,
) -> f64 {
    let momentum_skew = compute_skew(momentum_signal, yes_mid);
    let imbalance = calculate_orderbook_imbalance(yes_bid_depth, no_bid_depth);
    let imbalance_f = imbalance.to_f64().unwrap_or(0.0);

    let normalized_imbalance = imbalance_f * 2.0; // scale to ~ -2 to +2
    (momentum_skew * momentum_weight) + (normalized_imbalance * imbalance_weight)
}