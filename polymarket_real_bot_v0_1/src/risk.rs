use crate::{
    config::RiskConfig,
    paper::{PaperLedger, QuoteIntent},
};
use anyhow::{bail, Result};

pub struct RiskEngine {
    cfg: RiskConfig,
}

impl RiskEngine {
    pub fn new(cfg: RiskConfig) -> Self {
        Self { cfg }
    }

    pub fn check_quote(&self, quote: &mut QuoteIntent, ledger: &PaperLedger) -> Result<()> {
        // CLOB minimum order size
        const MIN_CLOB_SIZE: rust_decimal::Decimal = rust_decimal_macros::dec!(5);

        if quote.size_usdc <= rust_decimal::Decimal::ZERO {
            bail!("quote size must be positive");
        }

        // If size exceeds max, clamp it — but never below CLOB minimum
        if quote.size_usdc > self.cfg.max_order_usdc {
            let clamped = self.cfg.max_order_usdc;
            if clamped < MIN_CLOB_SIZE {
                bail!(
                    "max_order_usdc {} is below CLOB minimum {} — fix config",
                    clamped, MIN_CLOB_SIZE
                );
            }
            tracing::debug!(
                "quote size {} exceeds max_order_usdc {}, clamping to {}",
                quote.size_usdc,
                self.cfg.max_order_usdc,
                clamped,
            );
            quote.size_usdc = clamped;
        }

        // Reject orders that are below the CLOB minimum
        if quote.size_usdc < MIN_CLOB_SIZE {
            bail!(
                "quote size {} below CLOB minimum {} shares",
                quote.size_usdc, MIN_CLOB_SIZE
            );
        }

        if ledger.open_intent_count() >= self.cfg.max_open_intents {
            bail!("too many open quote intents");
        }

        let today = chrono::Utc::now().format("%Y-%m-%d").to_string();
        let current_trades = if ledger.last_trade_day == today { ledger.daily_trades } else { 0 };
        if current_trades >= self.cfg.max_daily_trades {
            bail!("max_daily_trades limit of {} reached for today", self.cfg.max_daily_trades);
        }

        let is_sell = quote.side == "SELL";

        // Sells reduce exposure and add cash; buys increase exposure and consume cash
        let exposure_delta = if is_sell {
            -quote.size_usdc
        } else {
            quote.size_usdc
        };
        let cash_delta = if is_sell {
            quote.size_usdc
        } else {
            -quote.size_usdc
        };

        let projected_total = ledger.total_exposure_usdc() + exposure_delta;
        if projected_total > self.cfg.max_total_exposure_usdc {
            bail!(
                "projected total exposure {} exceeds max_total_exposure_usdc {}",
                projected_total,
                self.cfg.max_total_exposure_usdc
            );
        }

        let projected_market = ledger.market_exposure_usdc(&quote.market_id) + exposure_delta;
        if projected_market > self.cfg.max_market_exposure_usdc {
            bail!(
                "projected market exposure {} exceeds max_market_exposure_usdc {}",
                projected_market,
                self.cfg.max_market_exposure_usdc
            );
        }

        let projected_cash = ledger.cash_usdc + cash_delta;
        if projected_cash < self.cfg.min_cash_buffer_usdc {
            bail!(
                "projected cash {} below min_cash_buffer_usdc {}",
                projected_cash,
                self.cfg.min_cash_buffer_usdc
            );
        }

        Ok(())
    }
}
