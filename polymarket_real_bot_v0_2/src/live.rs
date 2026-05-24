use anyhow::Result;
use rust_decimal::Decimal;
use tracing::info;

use crate::executor::LiveExecutor;

pub async fn setup_live_trading(clob_url: &str, max_order_usdc: Decimal) -> Result<LiveExecutor> {
    info!("Initializing LIVE TRADING EXECUTOR...");
    let executor = LiveExecutor::from_env(clob_url, max_order_usdc).await?;
    info!("LIVE TRADING EXECUTOR initialized successfully");
    Ok(executor)
}
