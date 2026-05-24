use crate::config::{FillMode, PaperConfig};
use anyhow::{Context, Result};
use chrono::{DateTime, Utc};
use rust_decimal::Decimal;
use rust_decimal_macros::dec;
use serde::{Deserialize, Serialize};
use std::{
    collections::HashMap,
    fs::{self, OpenOptions},
    io::Write,
    path::Path,
};

/// Whether an order should be placed as maker (post_only) or taker.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum OrderType {
    /// Maker order: 0% fee, rests on book (for arb / risk-free trades)
    Maker,
    /// Taker order: pays ~0.8% fee but fills immediately (for directional trades)
    Taker,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct QuoteIntent {
    pub market_id: String,
    pub condition_id: String,
    pub token_id: String,
    pub outcome: String,
    pub side: String,
    pub price: Decimal,
    pub size_usdc: Decimal,
    pub reason: String,
    #[serde(default = "default_order_type")]
    pub order_type: OrderType,
}

fn default_order_type() -> OrderType {
    OrderType::Taker
}

impl QuoteIntent {
    pub fn buy(
        market_id: String,
        condition_id: String,
        token_id: String,
        outcome: String,
        price: Decimal,
        size_usdc: Decimal,
        reason: String,
    ) -> Self {
        Self {
            market_id,
            condition_id,
            token_id,
            outcome,
            side: "BUY".to_string(),
            price,
            size_usdc,
            reason,
            order_type: OrderType::Taker,
        }
    }

    pub fn sell(
        market_id: String,
        condition_id: String,
        token_id: String,
        outcome: String,
        price: Decimal,
        size_usdc: Decimal,
        reason: String,
    ) -> Self {
        Self {
            market_id,
            condition_id,
            token_id,
            outcome,
            side: "SELL".to_string(),
            price,
            size_usdc,
            reason,
            order_type: OrderType::Taker,
        }
    }

    /// Convert this quote to a maker (post_only) order for arb execution.
    pub fn as_maker(mut self) -> Self {
        self.order_type = OrderType::Maker;
        self
    }

    pub fn shares(&self) -> Decimal {
        if self.price <= Decimal::ZERO {
            Decimal::ZERO
        } else {
            (self.size_usdc / self.price).round_dp(6)
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Position {
    pub market_id: String,
    pub token_id: String,
    pub outcome: String,
    pub shares: Decimal,
    pub cost_usdc: Decimal,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PaperEvent {
    pub timestamp: DateTime<Utc>,
    pub event_type: String,
    pub market_id: String,
    pub condition_id: String,
    pub token_id: String,
    pub outcome: String,
    pub side: String,
    pub price: Decimal,
    pub size_usdc: Decimal,
    pub shares: Decimal,
    pub reason: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PaperLedger {
    pub cash_usdc: Decimal,
    pub starting_cash_usdc: Decimal,
    pub positions: HashMap<String, Position>,
    pub open_intents: Vec<PaperEvent>,
    pub default_order_size_usdc: Decimal,
    pub updated_at: DateTime<Utc>,
    #[serde(default)]
    pub daily_trades: usize,
    #[serde(default)]
    pub last_trade_day: String,
}

impl PaperLedger {
    pub fn load_or_new(cfg: &PaperConfig) -> Result<Self> {
        let path = Path::new(&cfg.ledger_state_path);

        if path.exists() {
            let raw = fs::read_to_string(path)
                .with_context(|| format!("cannot read {}", cfg.ledger_state_path))?;
            let ledger: Self = serde_json::from_str(&raw)
                .with_context(|| format!("cannot parse {}", cfg.ledger_state_path))?;
            return Ok(ledger);
        }

        Ok(Self {
            cash_usdc: cfg.starting_cash_usdc,
            starting_cash_usdc: cfg.starting_cash_usdc,
            positions: HashMap::new(),
            open_intents: Vec::new(),
            default_order_size_usdc: dec!(5),
            updated_at: Utc::now(),
            daily_trades: 0,
            last_trade_day: Utc::now().format("%Y-%m-%d").to_string(),
        })
    }

    pub fn default_order_size_usdc(&self) -> Decimal {
        self.default_order_size_usdc
    }

    pub fn open_intent_count(&self) -> usize {
        self.open_intents.len()
    }

    pub fn total_exposure_usdc(&self) -> Decimal {
        self.positions
            .values()
            .map(|p| p.cost_usdc)
            .fold(Decimal::ZERO, |acc, x| acc + x)
            + self
                .open_intents
                .iter()
                .map(|e| e.size_usdc)
                .fold(Decimal::ZERO, |acc, x| acc + x)
    }

    pub fn market_exposure_usdc(&self, market_id: &str) -> Decimal {
        self.positions
            .values()
            .filter(|p| p.market_id == market_id)
            .map(|p| p.cost_usdc)
            .fold(Decimal::ZERO, |acc, x| acc + x)
            + self
                .open_intents
                .iter()
                .filter(|e| e.market_id == market_id)
                .map(|e| e.size_usdc)
                .fold(Decimal::ZERO, |acc, x| acc + x)
    }

    pub fn submit_quote_intent(
        &mut self,
        cfg: &PaperConfig,
        quote: QuoteIntent,
    ) -> Result<PaperEvent> {
        let event_type = match cfg.fill_mode {
            FillMode::RecordOnly => "QUOTE_INTENT",
            FillMode::Immediate => "PAPER_FILL",
        }
        .to_string();

        let today = Utc::now().format("%Y-%m-%d").to_string();
        if self.last_trade_day != today {
            self.last_trade_day = today;
            self.daily_trades = 0;
        }
        self.daily_trades += 1;

        let shares = quote.shares();
        let event = PaperEvent {
            timestamp: Utc::now(),
            event_type,
            market_id: quote.market_id.clone(),
            condition_id: quote.condition_id.clone(),
            token_id: quote.token_id.clone(),
            outcome: quote.outcome.clone(),
            side: quote.side.clone(),
            price: quote.price,
            size_usdc: quote.size_usdc,
            shares,
            reason: quote.reason.clone(),
        };

        match cfg.fill_mode {
            FillMode::RecordOnly => {
                self.open_intents.push(event.clone());
            }
            FillMode::Immediate => {
                self.apply_fill(&event);
            }
        }

        self.updated_at = Utc::now();
        self.append_event(cfg, &event)?;
        Ok(event)
    }

    fn apply_fill(&mut self, event: &PaperEvent) {
        let is_sell = event.side == "SELL";

        if is_sell {
            // Selling: receive cash, reduce position
            self.cash_usdc += event.size_usdc;

            if let Some(pos) = self.positions.get_mut(&event.token_id) {
                if pos.shares > Decimal::ZERO {
                    let fraction = (event.shares / pos.shares).min(Decimal::ONE);
                    pos.shares -= event.shares;
                    pos.cost_usdc -= (pos.cost_usdc * fraction).round_dp(2);
                }
                // Remove position if fully closed
                if pos.shares <= Decimal::ZERO {
                    self.positions.remove(&event.token_id);
                }
            }
        } else {
            // Buying: spend cash, increase position
            self.cash_usdc -= event.size_usdc;

            let entry = self.positions.entry(event.token_id.clone()).or_insert(Position {
                market_id: event.market_id.clone(),
                token_id: event.token_id.clone(),
                outcome: event.outcome.clone(),
                shares: Decimal::ZERO,
                cost_usdc: Decimal::ZERO,
            });

            entry.shares += event.shares;
            entry.cost_usdc += event.size_usdc;
        }
    }

    fn append_event(&self, cfg: &PaperConfig, event: &PaperEvent) -> Result<()> {
        let mut file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&cfg.ledger_events_path)
            .with_context(|| format!("cannot open {}", cfg.ledger_events_path))?;

        let line = serde_json::to_string(event)?;
        writeln!(file, "{line}")?;
        Ok(())
    }

    pub fn save_state(&self, cfg: &PaperConfig) -> Result<()> {
        let raw = serde_json::to_string_pretty(self)?;
        fs::write(&cfg.ledger_state_path, raw)
            .with_context(|| format!("cannot write {}", cfg.ledger_state_path))?;
        Ok(())
    }
}
