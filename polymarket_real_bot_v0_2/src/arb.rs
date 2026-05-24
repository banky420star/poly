use crate::{
    clob::OrderBookSummary,
    models::GammaMarket,
    paper::{PaperLedger, QuoteIntent},
};
use anyhow::Result;
use rust_decimal::Decimal;
use rust_decimal_macros::dec;

/// Pair-cost arbitrage: lock in risk-free profit by buying both YES and NO
/// when their combined cost is below $1.00 (after fees).
///
/// Example: YES bid = 0.48, NO bid = 0.48 → total = 0.96 → buy both
///   At resolution one token settles to $1, the other to $0.
///   Gross profit = $0.04 per pair.
///   Net = $0.04 - taker fees on both sides.
pub struct ArbEngine {
    /// Minimum combined bid below which we execute (e.g. 0.98 means $1.00 - 0.02 buffer for fees)
    max_combined_bid: Decimal,
    /// Maximum notional to deploy per arb opportunity
    max_size_usdc: Decimal,
}

impl ArbEngine {
    pub fn new(max_combined_bid: Decimal, max_size_usdc: Decimal) -> Self {
        Self {
            max_combined_bid,
            max_size_usdc,
        }
    }

    /// Scan a single market for an arb opportunity.
    /// Uses ASK prices — what we actually pay to buy — not bids.
    /// Returns (yes_quote, no_quote) if an arb is found, or None.
    pub fn check_arb(
        &self,
        market: &GammaMarket,
        yes_book: &OrderBookSummary,
        no_book: &OrderBookSummary,
        ledger: &PaperLedger,
    ) -> Option<(QuoteIntent, QuoteIntent)> {
        let yes_info = yes_book.spread_info();
        let no_info = no_book.spread_info();

        // Use ASK prices — these are the prices we pay to buy.
        // Bids are what we'd receive when selling — irrelevant for a buy arb.
        let yes_ask = yes_info.best_ask?;
        let no_ask = no_info.best_ask?;

        if yes_ask <= dec!(0) || no_ask <= dec!(0) {
            return None;
        }

        let combined = yes_ask + no_ask;

        // Only arb if combined cost is strictly below $1.00 (accounting for fees).
        // Buffer: combined <= 0.985 leaves 1.5% for 1.6% taker fees (tight).
        if combined >= self.max_combined_bid {
            return None;
        }

        // Size: equal notional on both sides, limited by depth and cash
        let depth = yes_info.depth_ask_usdc.min(no_info.depth_ask_usdc);
        let size = self.max_size_usdc.min(depth).min(ledger.cash_usdc / dec!(2));

        if size < dec!(1) {
            return None;
        }

        let yes_token = market.yes_token_id()?;
        let no_token = market.no_token_id()?;

        // Gross profit: buy at combined cost, receive $1.00 at resolution
        let gross_profit = size * (dec!(1) - combined) / combined;
        // Taker fee on both sides
        let estimated_fee = size * dec!(0.016);

        if gross_profit <= estimated_fee {
            return None;
        }

        let reason = format!(
            "ARB yes_ask={} no_ask={} combined={} gross={} net={}",
            yes_ask, no_ask, combined,
            gross_profit.round_dp(4),
            (gross_profit - estimated_fee).round_dp(4)
        );

        // Use taker orders for arb — the profitability check above (gross > fees)
        // already ensures we only execute when net-profitable. Taker fills both
        // legs immediately so we don't get lopsided exposure.
        let yes_quote = QuoteIntent::buy(
            market.id.clone(),
            market.condition_or_id(),
            yes_token,
            "YES".to_string(),
            yes_ask,
            size,
            reason.clone(),
        );

        let no_quote = QuoteIntent::buy(
            market.id.clone(),
            market.condition_or_id(),
            no_token,
            "NO".to_string(),
            no_ask,
            size,
            reason,
        );

        Some((yes_quote, no_quote))
    }
}
