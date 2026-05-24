use crate::{
    config::StrategyConfig,
    paper::{PaperLedger, QuoteIntent},
};
use rust_decimal::prelude::ToPrimitive;
use rust_decimal::Decimal;
use rust_decimal_macros::dec;
use tracing::info;

pub struct ExitManager {
    take_profit_bps: i64,
    stop_loss_bps: i64,
    min_hold_seconds: u64,
}

impl ExitManager {
    pub fn new(cfg: &StrategyConfig) -> Self {
        Self {
            take_profit_bps: cfg.take_profit_bps,
            stop_loss_bps: cfg.stop_loss_bps,
            min_hold_seconds: cfg.min_hold_seconds,
        }
    }

    /// Generate SELL quotes for positions that have hit take-profit or stop-loss.
    /// `best_bids` maps token_id → current best bid.
    /// `best_asks` maps token_id → current best ask (used with best_bid to compute mid).
    pub fn evaluate_exits(
        &self,
        ledger: &PaperLedger,
        best_bids: &std::collections::HashMap<String, Decimal>,
        best_asks: &std::collections::HashMap<String, Decimal>,
    ) -> Vec<QuoteIntent> {
        let mut exits = Vec::new();

        for (token_id, pos) in &ledger.positions {
            let best_bid = match best_bids.get(token_id) {
                Some(b) => *b,
                None => {
                    info!(%token_id, "exit: no best bid, skipping");
                    continue;
                }
            };

            let entry_price = if pos.shares > Decimal::ZERO {
                pos.cost_usdc / pos.shares
            } else {
                continue;
            };

            // P&L vs actual executable exit price (best_bid), not mid.
            // Polymarket binary markets have universal wide spreads (bid~0.001, ask~0.999).
            // Mid-price of ~0.5 is a mathematical artifact — you can only sell at best_bid.
            if best_bid < dec!(0.001) {
                continue;
            }

            let pnl_bps = ((best_bid - entry_price) / entry_price
                * Decimal::from(10000))
            .round_dp(0);

            let pnl_bps_i64 = pnl_bps
                .to_i64()
                .unwrap_or(0);

            let best_ask = best_asks.get(token_id).copied();
            info!(
                %token_id,
                entry = %entry_price,
                bid = %best_bid,
                ask = ?best_ask,
                pnl_bps = %pnl_bps_i64,
                tp = %self.take_profit_bps,
                sl = %self.stop_loss_bps,
                "exit eval"
            );

            let reason;
            if pnl_bps_i64 >= self.take_profit_bps {
                reason = format!(
                    "take_profit {}bps entry={} bid={}",
                    pnl_bps_i64, entry_price, best_bid
                );
            } else if pnl_bps_i64 <= -self.stop_loss_bps {
                reason = format!(
                    "stop_loss {}bps entry={} bid={}",
                    pnl_bps_i64, entry_price, best_bid
                );
            } else {
                continue;
            }

            let sell_price = best_bid.round_dp(3);
            let notional = (pos.shares * sell_price).round_dp(2);
            if notional <= Decimal::ZERO {
                continue;
            }

            // CLOB requires $5 minimum. If position is worth less, sell the
            // full position anyway — the CLOB will reject it and we log the attempt.
            // This is better than silently holding worthless dust.
            let final_notional = if notional < dec!(5) {
                tracing::info!(
                    %token_id,
                    notional = %notional,
                    "exit: notional below $5 min, will be rejected by CLOB"
                );
                notional
            } else {
                notional
            };

            // CLOB minimums: $1 notional, 5 shares. Skip dust positions.
            if notional < dec!(1) {
                info!(%token_id, notional = %notional, "exit: notional below CLOB $1 minimum, skipping");
                continue;
            }
            if pos.shares < dec!(5) {
                info!(%token_id, shares = %pos.shares, "exit: shares below CLOB 5 minimum, skipping");
                continue;
            }

            exits.push(QuoteIntent::sell(
                pos.market_id.clone(),
                String::new(),
                token_id.clone(),
                pos.outcome.clone(),
                sell_price,
                final_notional,
                reason,
            ));
        }

        exits
    }
}
