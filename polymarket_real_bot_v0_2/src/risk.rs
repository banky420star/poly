use crate::{
    config::{CompoundConfig, RiskConfig},
    paper::{PaperLedger, QuoteIntent},
};
use anyhow::{bail, Result};
use rust_decimal::prelude::{FromPrimitive, ToPrimitive};
use rust_decimal::Decimal;

pub struct RiskEngine {
    cfg: RiskConfig,
}

impl RiskEngine {
    pub fn new(cfg: RiskConfig) -> Self {
        Self { cfg }
    }

    /// Update risk limits based on current equity (compounding mode).
    /// When compound config is provided and enabled, all limits scale with equity.
    pub fn update_for_equity(&mut self, equity: Decimal, compound: Option<&CompoundConfig>) {
        if let Some(c) = compound {
            if c.enabled && equity > Decimal::ZERO {
                let eq = equity.to_f64().unwrap_or(0.0);
                // Floor at CLOB minimum $5 so tiny accounts can still trade
                self.cfg.max_order_usdc = Decimal::from_f64(eq * c.max_order_pct)
                    .unwrap_or(self.cfg.max_order_usdc)
                    .max(Decimal::from(5));
                self.cfg.max_total_exposure_usdc = Decimal::from_f64(eq * c.max_exposure_pct)
                    .unwrap_or(self.cfg.max_total_exposure_usdc)
                    .max(Decimal::from(5));
                // Per-market exposure: half of total exposure for diversification
                self.cfg.max_market_exposure_usdc = Decimal::from_f64(eq * c.max_exposure_pct * 0.5)
                    .unwrap_or(self.cfg.max_market_exposure_usdc)
                    .max(Decimal::from(5));
                self.cfg.min_cash_buffer_usdc = Decimal::from_f64(eq * c.min_cash_buffer_pct)
                    .unwrap_or(self.cfg.min_cash_buffer_usdc)
                    .min(Decimal::from(2));
            }
        }
    }

    pub fn check_quote(&self, quote: &mut QuoteIntent, ledger: &PaperLedger) -> Result<()> {
        // CLOB minimum order size
        const MIN_CLOB_SIZE: rust_decimal::Decimal = rust_decimal_macros::dec!(5);

        if quote.size_usdc <= rust_decimal::Decimal::ZERO {
            bail!("quote size must be positive");
        }

        // Reject orders that exceed the per-order cap. The strategy engine is
        // responsible for sizing quotes within limits; silently clamping would
        // execute a different trade than intended.
        if quote.size_usdc > self.cfg.max_order_usdc {
            bail!(
                "quote size {} exceeds max_order_usdc {} — strategy must respect risk limits",
                quote.size_usdc,
                self.cfg.max_order_usdc,
            );
        }

        // Reject orders that are below the CLOB minimum (5 shares)
        let shares = quote.size_usdc / quote.price;
        if shares < MIN_CLOB_SIZE {
            bail!(
                "quote size {:.2} USDC at price {:.2} is {:.2} shares, below CLOB minimum {} shares",
                quote.size_usdc, quote.price, shares, MIN_CLOB_SIZE
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

    /// Verify preconditions for live trading. Returns an error describing
    /// which gate failed, or Ok(()) if all clear.
    pub fn check_live_gate(
        &self,
        balance: Decimal,
        data_age_ms: u64,
        ws_connected: bool,
        feed_healthy: bool,
    ) -> Result<()> {
        if balance < self.cfg.live_min_balance_usdc {
            bail!(
                "live gate: balance ${} below minimum ${}",
                balance,
                self.cfg.live_min_balance_usdc
            );
        }
        if data_age_ms > self.cfg.max_data_age_ms {
            bail!(
                "live gate: data age {}ms exceeds max {}ms",
                data_age_ms,
                self.cfg.max_data_age_ms
            );
        }
        if !ws_connected {
            bail!("live gate: WebSocket not connected");
        }
        if !feed_healthy {
            bail!("live gate: price feed unhealthy");
        }
        Ok(())
    }

    /// Reserve for open orders: deducts the notional of resting orders
    /// from available balance to prevent over-committing.
    pub fn available_balance(&self, cash: Decimal, reserved: Decimal) -> Decimal {
        let after_reserve = cash - reserved;
        if after_reserve < self.cfg.min_cash_buffer_usdc {
            Decimal::ZERO
        } else {
            after_reserve - self.cfg.min_cash_buffer_usdc
        }
    }
}
