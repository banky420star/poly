use anyhow::{Context, Result};
use rust_decimal::Decimal;
use serde::Deserialize;
use std::{fs, path::Path};

#[derive(Debug, Clone, Deserialize)]
pub struct AppConfig {
    pub gamma_base_url: String,
    pub clob_base_url: String,
    pub dry_run_iterations: u64,
    pub loop_interval_ms: u64,
    pub paper: PaperConfig,
    pub risk: RiskConfig,
    pub strategy: StrategyConfig,
    pub fees: FeesConfig,
    pub market_filter: MarketFilterConfig,
}

impl AppConfig {
    pub fn from_file(path: impl AsRef<Path>) -> Result<Self> {
        let raw = fs::read_to_string(path.as_ref())
            .with_context(|| format!("cannot read config file {}", path.as_ref().display()))?;
        let cfg: Self = toml::from_str(&raw).context("invalid TOML config")?;
        Ok(cfg)
    }
}

#[derive(Debug, Clone, Deserialize)]
pub struct PaperConfig {
    pub starting_cash_usdc: Decimal,
    pub ledger_state_path: String,
    pub ledger_events_path: String,
    pub fill_mode: FillMode,
}

#[derive(Debug, Clone, Copy, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum FillMode {
    RecordOnly,
    Immediate,
}

#[derive(Debug, Clone, Deserialize)]
pub struct RiskConfig {
    pub max_total_exposure_usdc: Decimal,
    pub max_market_exposure_usdc: Decimal,
    pub max_order_usdc: Decimal,
    pub min_cash_buffer_usdc: Decimal,
    pub max_open_intents: usize,
    #[serde(default = "default_max_daily_trades")]
    pub max_daily_trades: usize,
}

fn default_max_daily_trades() -> usize {
    200_000
}

#[derive(Debug, Clone, Deserialize)]
pub struct StrategyConfig {
    pub name: String,
    pub min_yes_mid: Decimal,
    pub max_yes_mid: Decimal,
    pub maker_edge_bps: i64,
    pub quote_spread_bps: i64,
    pub allow_buy_yes: bool,
    pub allow_buy_no: bool,
    pub directional_load_window_seconds: u64,
    pub min_skew_ratio: f64,
    pub reference_feed: String,
    #[serde(default = "default_take_profit_bps")]
    pub take_profit_bps: i64,
    #[serde(default = "default_stop_loss_bps")]
    pub stop_loss_bps: i64,
    #[serde(default = "default_min_hold_seconds")]
    pub min_hold_seconds: u64,
}

#[derive(Debug, Clone, Deserialize)]
pub struct FeesConfig {
    pub maker_fee_bps: i64,
    pub taker_fee_bps: i64,
    pub prefer_maker: bool,
}

impl Default for FeesConfig {
    fn default() -> Self {
        Self {
            maker_fee_bps: 0,
            taker_fee_bps: 80,
            prefer_maker: true,
        }
    }
}

#[derive(Debug, Clone, Deserialize)]
pub struct MarketFilterConfig {
    pub limit: usize,
    pub require_active: bool,
    pub require_open: bool,
    pub keywords: Vec<String>,
    pub exclude_keywords: Vec<String>,
    #[serde(default = "default_min_depth_usdc")]
    pub min_depth_usdc: Decimal,
}

fn default_min_depth_usdc() -> Decimal {
    Decimal::from(50)
}

fn default_take_profit_bps() -> i64 {
    200
}

fn default_stop_loss_bps() -> i64 {
    500
}

fn default_min_hold_seconds() -> u64 {
    60
}
