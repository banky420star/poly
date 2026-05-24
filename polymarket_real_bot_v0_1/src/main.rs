mod clob;
mod config;
mod executor;
mod exit;
mod feed;
mod gamma;
mod live;
mod models;
mod paper;
mod poly_ws;
mod risk;
mod selector;
mod signing;
mod strategy;

use std::io::Write;

use anyhow::{bail, Context, Result};
use clap::{Parser, Subcommand};
use config::AppConfig;
use clob::ClobClient;
use feed::BinanceFeed;
use gamma::GammaClient;
use models::DepthData;
use paper::PaperLedger;
use poly_ws::PolyWsFeed;
use risk::RiskEngine;
use rust_decimal_macros::dec;
use rust_decimal::prelude::ToPrimitive;
use selector::MarketSelector;
use strategy::ReactiveDirectional;
use tracing::{info, warn};

#[derive(Parser, Debug)]
#[command(name = "polymarket-real-bot")]
#[command(about = "Chain Gambler v0.3 — Polymarket paper trading bot")]
struct Cli {
    #[command(subcommand)]
    command: Commands,
}

#[derive(Subcommand, Debug)]
enum Commands {
    /// Run the bot loop (paper mode).
    Run {
        #[arg(short, long, default_value = "config.toml")]
        config: String,
        #[arg(long, default_value_t = false)]
        paper: bool,
        #[arg(long, default_value_t = true)]
        live: bool,
    },
    /// Show live dashboard (watch mode). Run in a separate terminal.
    Dashboard {
        #[arg(short, long, default_value = "config.toml")]
        config: String,
    },
    /// List selected markets with order book data.
    Markets {
        #[arg(short, long, default_value = "config.toml")]
        config: String,
    },
    /// Show paper ledger status.
    Status {
        #[arg(short, long, default_value = "config.toml")]
        config: String,
    },
    /// Show order book depth for selected markets.
    Depth {
        #[arg(short, long, default_value = "config.toml")]
        config: String,
    },
}

// ANSI colors
const GREEN: &str = "\x1b[32m";
const RED: &str = "\x1b[31m";
const CYAN: &str = "\x1b[36m";
const YELLOW: &str = "\x1b[33m";
const BOLD: &str = "\x1b[1m";
const DIM: &str = "\x1b[2m";
const RESET: &str = "\x1b[0m";
const BG_DARK: &str = "\x1b[48;5;236m";

fn clear() {
    print!("\x1B[2J\x1B[H");
}

fn fmt_usdc(d: rust_decimal::Decimal) -> String {
    format!("${:.2}", d)
}

fn fmt_pct(d: rust_decimal::Decimal) -> String {
    format!("{:.2}%", d)
}

#[tokio::main]
async fn main() -> Result<()> {
    dotenvy::dotenv().ok();

    tracing_subscriber::fmt()
        .with_env_filter(
            std::env::var("RUST_LOG")
                .unwrap_or_else(|_| "warn,polymarket_real_bot=info".to_string()),
        )
        .init();

    let cli = Cli::parse();

    match cli.command {
        Commands::Run { config, paper, live } => run_bot(&config, paper, live).await,
        Commands::Dashboard { config } => run_dashboard(&config),
        Commands::Markets { config } => show_markets(&config).await,
        Commands::Status { config } => show_status(&config),
        Commands::Depth { config } => show_depth(&config).await,
    }
}

fn print_dashboard(
    iteration: u64,
    ref_price: rust_decimal::Decimal,
    pct_change: f64,
    momentum_signal: f64,
    selected_count: usize,
    total_markets: usize,
    buys: usize,
    sells: usize,
    rejected: usize,
    ledger: &PaperLedger,
    equity: rust_decimal::Decimal,
    depth_filtered: usize,
    live: bool,
) {
    let cash = ledger.cash_usdc;
    let starting = ledger.starting_cash_usdc;
    let pnl = equity - starting;
    let pnl_pct = if starting > rust_decimal::Decimal::ZERO {
        (pnl / starting) * rust_decimal::Decimal::from(100)
    } else {
        rust_decimal::Decimal::ZERO
    };

    let pnl_color = if pnl >= rust_decimal::Decimal::ZERO { GREEN } else { RED };
    let momentum_color = if momentum_signal > 0.55 { GREEN } else if momentum_signal < 0.45 { RED } else { YELLOW };
    let change_color = if pct_change > 0.0 { GREEN } else if pct_change < 0.0 { RED } else { YELLOW };

    let (direction, dir_color) = if momentum_signal > 0.55 {
        ("BTC ↑  →  BUY YES", GREEN)
    } else if momentum_signal < 0.45 {
        ("BTC ↓  →  BUY NO", RED)
    } else {
        ("BTC →  →  HOLD", YELLOW)
    };

    let mode_str = if live {
        format!("{BG_DARK}{BOLD}{RED}  CHAIN GAMBLER v0.3 — LIVE TRADING  {RESET}")
    } else {
        format!("{BG_DARK}{BOLD}  CHAIN GAMBLER v0.3 — PAPER MODE  {RESET}")
    };

    // clear();
    println!("{}", mode_str);
    println!();
    println!("  {CYAN}Loop #{iteration:<5}{RESET}  {BOLD}BTC/USDT  ${ref_price}{RESET}  {change_color}{:+.2}%{RESET}", pct_change * 100.0);
    println!("  {BOLD}Momentum:{RESET}  {momentum_color}{momentum_signal:.3}{RESET}  {dir_color}{BOLD}{direction}{RESET}");
    println!("  Markets:   {selected_count} selected / {total} total / {depth_filtered} thin-depth skipped", total = total_markets);
    println!();
    println!("  {BOLD}╔══════════════════════════════════════════╗{RESET}");
    println!("  {BOLD}║          BALANCE & EQUITY               ║{RESET}");
    println!("  {BOLD}╚══════════════════════════════════════════╝{RESET}");
    println!("  {BOLD}  Cash:{RESET}      {:>12}", fmt_usdc(cash));
    println!("  {BOLD}  Equity:{RESET}    {:>12}  (cash + positions)", fmt_usdc(equity));
    println!("  {BOLD}  P&L:{RESET}     {pnl_color}{:>12}  ({}){RESET}", fmt_usdc(pnl), fmt_pct(pnl_pct));
    println!("  {BOLD}  Exposure:{RESET}  {:>12}", fmt_usdc(ledger.total_exposure_usdc()));
    println!();
    println!("  {BOLD}╔══════════════════════════════════════════╗{RESET}");
    println!("  {BOLD}║          FLOW THIS LOOP                ║{RESET}");
    println!("  {BOLD}╚══════════════════════════════════════════╝{RESET}");
    println!("  {GREEN}▲ Buys:{RESET}     {:>4}    {RED}▼ Sells:{RESET}  {:>4}", buys, sells);
    println!("  {YELLOW}✗ Rejected:{RESET} {:>4}", rejected);
    println!();

    if !ledger.positions.is_empty() {
        println!("  {BOLD}╔════════════════════════════════════════════════════════════╗{RESET}");
        println!("  {BOLD}║          OPEN POSITIONS ({:>3})                              ║{RESET}", ledger.positions.len());
        println!("  {BOLD}╚════════════════════════════════════════════════════════════╝{RESET}");
        for pos in ledger.positions.values() {
            let (label, color) = if pos.outcome == "YES" {
                ("🟢 YES", GREEN)
            } else {
                ("🔴 NO ", RED)
            };
            let avg_price = if pos.shares > rust_decimal::Decimal::ZERO {
                pos.cost_usdc / pos.shares
            } else {
                rust_decimal::Decimal::ZERO
            };
            println!(
                "  {color}{label}{RESET}  {:>8} shares  @ ${:.4}  Cost: {:>10}",
                pos.shares.round_dp(2),
                avg_price,
                fmt_usdc(pos.cost_usdc),
            );
        }
        println!();
    }

    if !ledger.open_intents.is_empty() {
        let recent: Vec<_> = ledger.open_intents.iter().rev().take(8).collect();
        println!();
        println!("  {BOLD}╔══════════════════════════════════════════╗{RESET}");
        println!("  {BOLD}║          RECENT INTENTS (last 8)        ║{RESET}");
        println!("  {BOLD}╚══════════════════════════════════════════╝{RESET}");
        for intent in recent {
            let side_color = if intent.side == "BUY" { GREEN } else { RED };
            let outcome_color = if intent.outcome == "YES" { GREEN } else { RED };
            println!(
                "  {side_color}{:<4}{RESET} {outcome_color}{:>3}{RESET} @ ${:.4}  ${:.2}  {}",
                intent.side,
                intent.outcome,
                intent.price,
                intent.size_usdc,
                intent.reason.chars().take(40).collect::<String>(),
            );
        }
    }

    println!();
    println!("  {DIM}───────────────────────────────────────────{RESET}");
    println!("  {DIM}Ctrl+C to stop  |  'cargo run -- depth' for book depth{RESET}");
}

async fn run_bot(config_path: &str, paper: bool, live: bool) -> Result<()> {
    let cfg = AppConfig::from_file(config_path)
        .with_context(|| format!("failed to load config from {config_path}"))?;

    let mut live_executor = if live {
        Some(live::setup_live_trading(&cfg.clob_base_url, cfg.risk.max_order_usdc).await?)
    } else {
        None
    };

    if !paper && !live {
        bail!("You must specify either --paper or --live");
    }

    let gamma = GammaClient::with_clob(cfg.gamma_base_url.clone(), cfg.clob_base_url.clone())?;
    let clob = ClobClient::new(cfg.clob_base_url.clone())?;
    let selector = MarketSelector::new(cfg.market_filter.clone());
    let strategy = ReactiveDirectional::new(cfg.strategy.clone(), cfg.fees.clone());
    let exit_manager = exit::ExitManager::new(&cfg.strategy);
    let risk = RiskEngine::new(cfg.risk.clone());
    let mut ledger = PaperLedger::load_or_new(&cfg.paper)?;

    // In live mode, fetch real USDC balance and override the paper ledger
    if let Some(exec) = &live_executor {
        match exec.get_balance().await {
            Ok(balance) if balance > rust_decimal::Decimal::ZERO => {
                info!(balance = %balance, "Fetched real CLOB balance");
                ledger.cash_usdc = balance;
                ledger.starting_cash_usdc = balance;
                // Clear stale paper intents in live mode
                ledger.open_intents.clear();
                ledger.positions.clear();
            }
            Ok(_) => warn!("CLOB balance returned 0 — check your wallet or API credentials"),
            Err(e) => warn!("Could not fetch CLOB balance: {} — using paper defaults", e),
        }
    }

    let feed_symbol = match cfg.strategy.reference_feed.as_str() {
        "binance" => "btcusdt",
        other => {
            warn!("unknown reference_feed '{}', defaulting to btcusdt", other);
            "btcusdt"
        }
    };
    let mut feed = BinanceFeed::new(feed_symbol).await?;
    info!("Binance {} feed started", feed_symbol);

    let mut poly_ws: Option<PolyWsFeed> = None;
    let mut iteration: u64 = 0;
    let mut discovered_markets: std::collections::HashSet<String> = std::collections::HashSet::new();

    loop {
        iteration += 1;
        let (pct_change, momentum_signal) = feed.momentum_signal();
        let ref_price = feed.latest_price();
        let markets = gamma.active_markets(cfg.market_filter.limit).await?;
        let selected = selector.select(&markets);
        let selected_count = selected.len();

        if poly_ws.is_none() && !selected.is_empty() {
            let token_ids: Vec<String> = selected
                .iter()
                .filter_map(|m| m.clob_token_ids.as_ref())
                .flatten()
                .cloned()
                .collect();
            if !token_ids.is_empty() {
                match PolyWsFeed::new(token_ids.clone()).await {
                    Ok(ws) => {
                        info!(tokens = token_ids.len(), "Polymarket WS started with real tokens");
                        poly_ws = Some(ws);
                    }
                    Err(e) => warn!("Polymarket WS failed: {}", e),
                }
            }
        }

        let mut buys: usize = 0;
        let mut sells: usize = 0;
        let mut rejected: usize = 0;

        // Cancel all open orders from the previous loop to prevent accumulation.
        // Without this, each loop adds more resting orders until we have hundreds
        // of stale orders tying up balance.
        if let Some(exec) = &live_executor {
            if let Err(e) = exec.cancel_all_orders().await {
                tracing::warn!("failed to cancel old orders: {}", e);
            }
        }
        let mut depth_filtered: usize = 0;
        let min_depth = cfg.market_filter.min_depth_usdc;
        let mut best_bids: std::collections::HashMap<String, rust_decimal::Decimal> =
            std::collections::HashMap::new();
        let mut best_asks: std::collections::HashMap<String, rust_decimal::Decimal> =
            std::collections::HashMap::new();

        for market in selected.iter() {
            let cid = market.condition_id.clone().unwrap_or_else(|| "unknown".to_string());
            if !discovered_markets.contains(&cid) {
                discovered_markets.insert(cid.clone());
                log_system_event("MARKET", &format!("Discovered {}", market.question));
            }

            let book_data = match clob.get_orderbooks_for_market(market).await {
                Ok(b) => Some(b),
                Err(e) => {
                    log_system_event("CLOB", &format!("Failed to fetch order book for market_id={}: {}", cid, e));
                    None
                }
            };

            // Collect best bids/asks for exit evaluation
            if let Some(ref books) = book_data {
                for (token_id, book) in books {
                    let info = book.spread_info();
                    if let Some(bid) = info.best_bid {
                        best_bids.insert(token_id.clone(), bid);
                    }
                    if let Some(ask) = info.best_ask {
                        best_asks.insert(token_id.clone(), ask);
                    }
                }
            }

            // Build depth data from CLOB books
            let depth = build_depth_data(market, book_data.as_ref());

            // Skip expired or nearly-expired markets (keep 30s buffer for 5m markets)
            let time_left = strategy::time_left_seconds(market);
            if time_left.map_or(true, |t| t < 30) {
                continue;
            }

            // Depth filter: skip markets with insufficient liquidity on the side we'd trade
            let yes_mid = market.yes_mid().unwrap_or(dec!(0.5));
            let ask_depth = depth.as_ref().map_or(dec!(0), |d| {
                if yes_mid > dec!(0.5) { d.yes_ask_depth_usdc } else { d.no_ask_depth_usdc }
            });
            if ask_depth < min_depth && ask_depth > rust_decimal::Decimal::ZERO {
                depth_filtered += 1;
                continue;
            }

            let ws_snapshot = poly_ws.as_ref().map(|ws| ws.snapshot());
            let ws_books = ws_snapshot.as_ref().map(|s| s.all_snapshots());

            let quotes = strategy.build_quotes(
                market, &ledger, momentum_signal, time_left, ws_books, depth.as_ref(),
            )?;

            if quotes.is_empty() {
                log_market_watch(market, "-", 0.0, 0.0, 0.0, "WATCHING");
            }

            for mut quote in quotes {
                match risk.check_quote(&mut quote, &ledger) {
                    Ok(()) => {
                        log_market_watch(market, &quote.outcome, quote.price.to_f64().unwrap_or(0.0), quote.price.to_f64().unwrap_or(0.0), quote.price.to_f64().unwrap_or(0.0), "ENTRY");
                        // Place CLOB order first; only record paper fill if it actually matches
                        if let Some(exec) = &mut live_executor {
                            match exec.execute_order(&quote).await {
                                Ok(result) => {
                                    if result.filled {
                                        tracing::info!(
                                            order_id = %result.order_id,
                                            filled = %result.size_matched,
                                            "live order filled"
                                        );
                                        let event = ledger.submit_quote_intent(&cfg.paper, quote.clone())?;
                                        if event.side == "BUY" { buys += 1; } else { sells += 1; }
                                        // Log live fill to file
                                        log_live_fill(&result.order_id, &quote, &result.size_matched);
                                    } else {
                                        tracing::debug!(
                                            order_id = %result.order_id,
                                            "live order resting on book"
                                        );
                                        // Log resting order too
                                        log_live_order(&result.order_id, &quote, "RESTING");
                                    }
                                }
                                Err(e) => tracing::error!("live order failed: {}", e),
                            }
                        } else {
                            // Paper-only mode: record fill immediately
                            let event = ledger.submit_quote_intent(&cfg.paper, quote.clone())?;
                            if event.side == "BUY" { buys += 1; } else { sells += 1; }
                        }
                    }
                    Err(e) => {
                        rejected += 1;
                        let e_str = e.to_string();
                        if e_str.contains("exceeds max") {
                            log_system_event("RISK", &format!("Entry blocked: {}", e_str));
                            log_market_watch(market, &quote.outcome, quote.price.to_f64().unwrap_or(0.0), quote.price.to_f64().unwrap_or(0.0), quote.price.to_f64().unwrap_or(0.0), "BLOCKED");
                        } else {
                            log_market_watch(market, &quote.outcome, quote.price.to_f64().unwrap_or(0.0), quote.price.to_f64().unwrap_or(0.0), quote.price.to_f64().unwrap_or(0.0), "REJECTED");
                        }
                        println!("⚠️  Quote Rejected by Risk Engine: {}", e_str);
                    }
                }
            }
        }
        
        // Exit management: check open positions for take-profit / stop-loss
        if !ledger.positions.is_empty() && !best_bids.is_empty() {
            let exit_quotes = exit_manager.evaluate_exits(&ledger, &best_bids, &best_asks);
            for mut quote in exit_quotes {
                match risk.check_quote(&mut quote, &ledger) {
                    Ok(()) => {
                        if let Some(exec) = &mut live_executor {
                            match exec.execute_order(&quote).await {
                                Ok(result) => {
                                    if result.filled {
                                        tracing::info!(
                                            order_id = %result.order_id,
                                            "exit order filled — recording paper fill"
                                        );
                                        let _event = ledger.submit_quote_intent(&cfg.paper, quote.clone())?;
                                        sells += 1;
                                    } else {
                                        tracing::debug!("exit order resting on book");
                                    }
                                }
                                Err(e) => {
                                    tracing::error!("exit order failed: {}", e);
                                }
                            }
                        } else {
                            // Paper-only mode
                            let _event = ledger.submit_quote_intent(&cfg.paper, quote.clone())?;
                            sells += 1;
                        }
                    }
                    Err(e) => {
                        rejected += 1;
                        let e_str = e.to_string();
                        if e_str.contains("below CLOB minimum") {
                            log_system_event("EXIT", &format!("Exit skipped: {}", e_str));
                        }
                        tracing::warn!("Exit rejected by risk: {}", e_str);
                    }
                }
            }
        } else if !ledger.positions.is_empty() {
            tracing::info!(
                positions = ledger.positions.len(),
                bids = best_bids.len(),
                "exit skipped: positions exist but no best bids available"
            );
            log_system_event("EXIT", "Exit skipped: positions exist but no best bids available");
        }

        if let Some(exec) = &mut live_executor {
            if let Err(e) = exec.send_heartbeat().await {
                tracing::warn!("heartbeat failed: {}", e);
            }
        }

        ledger.save_state(&cfg.paper)?;

        // In live mode, refresh real balance every 5 loops
        let mut live_balance = ledger.cash_usdc;
        if live && iteration % 5 == 0 {
            if let Some(exec) = &live_executor {
                if let Ok(balance) = exec.get_balance().await {
                    if balance > rust_decimal::Decimal::ZERO {
                        ledger.cash_usdc = balance;
                        live_balance = balance;
                    }
                }
            }
        }

        // Write live dashboard JSON for the external dashboard script
        // Estimate equity from positions (use cost as proxy in record_only mode)
        let position_value: rust_decimal::Decimal = ledger.positions.values()
            .map(|p| p.cost_usdc)
            .sum();
        let equity = ledger.cash_usdc + position_value;

        if live {
            if let Some(exec) = &live_executor {
                write_live_dashboard(
                    iteration, live_balance, &ledger, equity,
                    buys, sells, rejected, exec.orders_placed, exec.orders_failed,
                    selected_count, markets.len(),
                    ref_price.to_f64().unwrap_or(0.0),
                    pct_change.to_f64().unwrap_or(0.0),
                    momentum_signal.to_f64().unwrap_or(0.0),
                    depth_filtered,
                    &cfg,
                    &selected,
                );
            }
        }

        print_dashboard(
            iteration, ref_price, pct_change, momentum_signal,
            selected_count, markets.len(),
            buys, sells, rejected, &ledger, equity, depth_filtered,
            live,
        );

        if cfg.dry_run_iterations > 0 && iteration >= cfg.dry_run_iterations {
            break;
        }

        tokio::select! {
            _ = tokio::time::sleep(std::time::Duration::from_millis(cfg.loop_interval_ms)) => {}
            _ = tokio::signal::ctrl_c() => {
                tracing::info!("Ctrl+C received. Initiating kill switch...");
                if let Some(exec) = &mut live_executor {
                    tracing::info!("Canceling all open orders on CLOB...");
                    if let Err(e) = exec.cancel_all_orders().await {
                        tracing::warn!("Failed to cancel all orders: {}", e);
                    } else {
                        tracing::info!("All open orders canceled successfully.");
                    }
                }
                break;
            }
        }
    }

    Ok(())
}

fn run_dashboard(config_path: &str) -> Result<()> {
    let cfg = AppConfig::from_file(config_path)?;
    loop {
        let ledger = PaperLedger::load_or_new(&cfg.paper)?;
        let position_value: rust_decimal::Decimal = ledger.positions.values()
            .map(|p| p.cost_usdc)
            .sum();
        let equity = ledger.cash_usdc + position_value;
        let pnl = equity - ledger.starting_cash_usdc;
        let pnl_pct = if ledger.starting_cash_usdc > rust_decimal::Decimal::ZERO {
            (pnl / ledger.starting_cash_usdc) * rust_decimal::Decimal::from(100)
        } else {
            rust_decimal::Decimal::ZERO
        };
        let pnl_color = if pnl >= rust_decimal::Decimal::ZERO { GREEN } else { RED };

        let buy_count = ledger.open_intents.iter().filter(|e| e.side == "BUY").count();
        let sell_count = ledger.open_intents.iter().filter(|e| e.side == "SELL").count();

        clear();
        println!("{BOLD}{CYAN}  CHAIN GAMBLER v0.3 — WATCH MODE{RESET}");
        println!("  {DIM}Auto-refreshing every 3s — Ctrl+C to exit{RESET}");
        println!();
        println!("  {BOLD}╔══════════════════════════════════════════╗{RESET}");
        println!("  {BOLD}║          BALANCE & EQUITY               ║{RESET}");
        println!("  {BOLD}╚══════════════════════════════════════════╝{RESET}");
        println!("  {BOLD}  Cash:{RESET}      {:>12}", fmt_usdc(ledger.cash_usdc));
        println!("  {BOLD}  Equity:{RESET}    {:>12}", fmt_usdc(equity));
        println!("  {BOLD}  P&L:{RESET}     {pnl_color}{:>12}  ({}){RESET}", fmt_usdc(pnl), fmt_pct(pnl_pct));
        println!("  {BOLD}  Exposure:{RESET}  {:>12}", fmt_usdc(ledger.total_exposure_usdc()));
        println!();
        println!("  {BOLD}╔══════════════════════════════════════════╗{RESET}");
        println!("  {BOLD}║          INTENT SUMMARY                ║{RESET}");
        println!("  {BOLD}╚══════════════════════════════════════════╝{RESET}");
        println!("  {GREEN}▲ Buys:{RESET}     {:>4}    {RED}▼ Sells:{RESET}  {:>4}    Total: {:>4}", buy_count, sell_count, ledger.open_intents.len());
        println!();

        if !ledger.positions.is_empty() {
            println!("  {BOLD}╔══════════════════════════════════════════╗{RESET}");
            println!("  {BOLD}║          OPEN POSITIONS ({:>3})           ║{RESET}", ledger.positions.len());
            println!("  {BOLD}╚══════════════════════════════════════════╝{RESET}");
            for pos in ledger.positions.values() {
                let outcome_color = if pos.outcome == "YES" { GREEN } else { RED };
                println!(
                    "  {outcome_color}{:>3}{RESET}  {:>8} shares  cost={:>8}",
                    pos.outcome,
                    pos.shares.round_dp(2),
                    fmt_usdc(pos.cost_usdc),
                );
            }
        }

        if !ledger.open_intents.is_empty() {
            let recent: Vec<_> = ledger.open_intents.iter().rev().take(12).collect();
            println!();
            println!("  {BOLD}╔══════════════════════════════════════════╗{RESET}");
            println!("  {BOLD}║          LAST 12 INTENTS                ║{RESET}");
            println!("  {BOLD}╚══════════════════════════════════════════╝{RESET}");
            for intent in recent {
                let side_color = if intent.side == "BUY" { GREEN } else { RED };
                let outcome_color = if intent.outcome == "YES" { GREEN } else { RED };
                println!(
                    "  {side_color}{:<4}{RESET} {outcome_color}{:>3}{RESET} @ ${:.4}  ${:.2}",
                    intent.side,
                    intent.outcome,
                    intent.price,
                    intent.size_usdc,
                );
            }
        }

        println!();
        println!("  {DIM}────────────────────────────────────────────{RESET}");
        println!("  {DIM}Updated: {}{RESET}", ledger.updated_at.format("%H:%M:%S"));

        std::thread::sleep(std::time::Duration::from_secs(3));
    }
}

async fn show_markets(config_path: &str) -> Result<()> {
    let cfg = AppConfig::from_file(config_path)?;
    let gamma = GammaClient::with_clob(cfg.gamma_base_url.clone(), cfg.clob_base_url.clone())?;
    let clob = ClobClient::new(cfg.clob_base_url.clone())?;
    let selector = MarketSelector::new(cfg.market_filter.clone());

    let markets = gamma.active_markets(cfg.market_filter.limit).await?;
    let selected = selector.select(&markets);

    println!("Found {} markets ({} selected):\n", markets.len(), selected.len());

    for market in selected {
        let time_left = strategy::time_left_seconds(market);
        let spread_info = if let Some(token_id) = market.yes_token_id() {
            match clob.get_orderbook(&token_id).await {
                Ok(book) => {
                    let info = book.spread_info();
                    format!(" bid={:?} ask={:?} spread={}bps", info.best_bid, info.best_ask, info.spread_bps.unwrap_or(0))
                }
                Err(_) => String::new(),
            }
        } else { String::new() };

        println!(
            "{} | {} | yes_mid={:?} | time_left={:?} |{}",
            market.id, market.question, market.yes_mid(), time_left, spread_info,
        );
    }
    Ok(())
}

fn show_status(config_path: &str) -> Result<()> {
    let cfg = AppConfig::from_file(config_path)?;
    let ledger = PaperLedger::load_or_new(&cfg.paper)?;
    println!("{}", serde_json::to_string_pretty(&ledger)?);
    Ok(())
}

/// Build depth data from CLOB order book summaries.
fn build_depth_data(
    market: &models::GammaMarket,
    books: Option<&std::collections::HashMap<String, clob::OrderBookSummary>>,
) -> Option<DepthData> {
    let books = books?;
    let yes_token = market.yes_token_id()?;
    let no_token = market.no_token_id()?;

    let yes_info = books.get(&yes_token).map(|b| b.spread_info());
    let no_info = books.get(&no_token).map(|b| b.spread_info());

    Some(DepthData {
        yes_bid_depth_usdc: yes_info.as_ref().map_or(dec!(0), |i| i.depth_bid_usdc),
        yes_ask_depth_usdc: yes_info.as_ref().map_or(dec!(0), |i| i.depth_ask_usdc),
        no_bid_depth_usdc: no_info.as_ref().map_or(dec!(0), |i| i.depth_bid_usdc),
        no_ask_depth_usdc: no_info.as_ref().map_or(dec!(0), |i| i.depth_ask_usdc),
        yes_best_bid: yes_info.as_ref().and_then(|i| i.best_bid),
        yes_best_ask: yes_info.as_ref().and_then(|i| i.best_ask),
        no_best_bid: no_info.as_ref().and_then(|i| i.best_bid),
        no_best_ask: no_info.as_ref().and_then(|i| i.best_ask),
        yes_levels: books.get(&yes_token).map_or(0, |b| b.bids.len() + b.asks.len()),
        no_levels: books.get(&no_token).map_or(0, |b| b.bids.len() + b.asks.len()),
    })
}

async fn show_depth(config_path: &str) -> Result<()> {
    let cfg = AppConfig::from_file(config_path)?;
    let gamma = GammaClient::with_clob(cfg.gamma_base_url.clone(), cfg.clob_base_url.clone())?;
    let clob = ClobClient::new(cfg.clob_base_url.clone())?;
    let selector = MarketSelector::new(cfg.market_filter.clone());

    let markets = gamma.active_markets(cfg.market_filter.limit).await?;
    let selected = selector.select(&markets);

    println!("{BOLD}CHAIN GAMBLER v0.3 — ORDER BOOK DEPTH{RESET}\n");
    println!("Found {} markets ({} selected):\n", markets.len(), selected.len());

    for market in selected {
        let yes_mid = market.yes_mid();
        let time_left = strategy::time_left_seconds(market);
        let question: String = market.question.chars().take(70).collect();

        println!("{BOLD}{}{RESET}", question);
        println!("  ID: {} | yes_mid={:?} | time_left={:?}",
            market.id, yes_mid, time_left);

        let books_opt = clob.get_orderbooks_for_market(market).await.ok();

        if let Some(ref books) = books_opt {
            if let Some(yes_token) = market.yes_token_id() {
                if let Some(yes_book) = books.get(&yes_token) {
                    let info = yes_book.spread_info();
                    println!("  {GREEN}YES{RESET}  bid={:?}  ask={:?}  spread={}bps  depth_bid=${}  depth_ask=${}  levels={}",
                        info.best_bid, info.best_ask,
                        info.spread_bps.unwrap_or(0),
                        info.depth_bid_usdc.round_dp(2),
                        info.depth_ask_usdc.round_dp(2),
                        yes_book.bids.len() + yes_book.asks.len(),
                    );
                    println!("    {DIM}Bids:{RESET}");
                    for level in yes_book.bids.iter().take(5) {
                        let price: rust_decimal::Decimal = level.price.parse().unwrap_or_default();
                        let size: rust_decimal::Decimal = level.size.parse().unwrap_or_default();
                        println!("      ${:.4} x {:.2}", price, size);
                    }
                    println!("    {DIM}Asks:{RESET}");
                    for level in yes_book.asks.iter().take(5) {
                        let price: rust_decimal::Decimal = level.price.parse().unwrap_or_default();
                        let size: rust_decimal::Decimal = level.size.parse().unwrap_or_default();
                        println!("      ${:.4} x {:.2}", price, size);
                    }
                }
            }

            if let Some(no_token) = market.no_token_id() {
                if let Some(no_book) = books.get(&no_token) {
                    let info = no_book.spread_info();
                    println!("  {RED}NO {RESET}  bid={:?}  ask={:?}  spread={}bps  depth_bid=${}  depth_ask=${}  levels={}",
                        info.best_bid, info.best_ask,
                        info.spread_bps.unwrap_or(0),
                        info.depth_bid_usdc.round_dp(2),
                        info.depth_ask_usdc.round_dp(2),
                        no_book.bids.len() + no_book.asks.len(),
                    );
                }
            }
        } else {
            println!("  {YELLOW}(no book data available){RESET}");
        }

        let depth = build_depth_data(market, books_opt.as_ref());
        if let Some(d) = depth {
            let total = d.yes_ask_depth_usdc + d.no_ask_depth_usdc;
            let status = if total < cfg.market_filter.min_depth_usdc {
                format!("{RED}THIN{RESET}")
            } else {
                format!("{GREEN}OK{RESET}")
            };
            println!("  Depth check: {} (total ask depth: ${})", status, total.round_dp(2));
        }

        println!();
    }

    Ok(())
}

/// Write a JSON snapshot of the live trading state for the external dashboard.
#[allow(clippy::too_many_arguments)]
fn write_live_dashboard(
    iteration: u64,
    clob_balance: rust_decimal::Decimal,
    ledger: &PaperLedger,
    equity: rust_decimal::Decimal,
    buys: usize,
    sells: usize,
    rejected: usize,
    total_orders_placed: u64,
    total_orders_failed: u64,
    selected_markets: usize,
    total_markets: usize,
    ref_price: f64,
    pct_change: f64,
    momentum_signal: f64,
    depth_filtered: usize,
    cfg: &crate::config::AppConfig,
    selected_markets_list: &[&crate::models::GammaMarket],
) {
    let now = chrono::Utc::now().to_rfc3339();
    let position_count = ledger.positions.len();
    let position_value: rust_decimal::Decimal = ledger.positions.values()
        .map(|p| p.cost_usdc)
        .sum();

    let positions_json: Vec<String> = ledger.positions.values().map(|p| {
        format!(
            r#"{{"outcome":"{}","shares":"{}","cost_usdc":"{}","market_id":"{}","token_id":"{}"}}"#,
            p.outcome, p.shares, p.cost_usdc, p.market_id, p.token_id
        )
    }).collect();

    let market_logs_json: Vec<String> = selected_markets_list.iter().take(6).map(|m| {
        let title = m.question.replace('"', "\\\"");
        let price = m.yes_mid().unwrap_or(rust_decimal_macros::dec!(0.5)).to_f64().unwrap_or(0.5);
        format!(r#"{{"title":"{}","price":{}}}"#, title, price)
    }).collect();

    // Determine signal label
    let signal = if momentum_signal > 0.53 {
        "BUY"
    } else if momentum_signal < 0.47 {
        "SELL"
    } else {
        "HOLD"
    };

    // Compute realized P&L from initial cash
    let starting_cash = ledger.starting_cash_usdc;
    let realized_pnl = equity - starting_cash;
    let pnl_pct = if starting_cash > rust_decimal::Decimal::ZERO {
        (realized_pnl / starting_cash * rust_decimal::Decimal::from(100))
            .to_f64().unwrap_or(0.0)
    } else {
        0.0
    };

    // Fill stats from fills file
    let (total_fills, fill_volume) = count_fills();
    let fill_rate = if total_orders_placed > 0 {
        total_fills as f64 / total_orders_placed as f64 * 100.0
    } else {
        0.0
    };

    // Config health
    let max_order = cfg.risk.max_order_usdc;
    let min_clob_size = rust_decimal::Decimal::from(5); // CLOB enforced minimum
    let config_valid = max_order >= min_clob_size;

    let json = format!(
        r#"{{"version":"0.5","timestamp":"{}","iteration":{},"clob_balance":"{}","equity":"{}","position_value":"{}","position_count":{},"positions":[{}],"market_logs":[{}],"btc_price":{:.2},"btc_change_pct":{:.4},"momentum":{:.4},"signal":"{}","loop_buys":{},"loop_sells":{},"loop_rejected":{},"orders_placed":{},"orders_failed":{},"total_fills":{},"fill_volume":{:.2},"fill_rate":{:.1},"selected_markets":{},"total_markets":{},"thin_depth_skipped":{},"max_order_usdc":"{}","min_clob_size":"5","max_exposure_usdc":"{}","min_cash_buffer":"{}","realized_pnl":"{}","pnl_pct":{:.2},"config_valid":{},"reject_min_size":0,"reject_notional":0,"reject_balance":0,"reject_exposure":0,"last_error":""}}"#,
        now, iteration, clob_balance, equity, position_value, position_count,
        positions_json.join(","),
        market_logs_json.join(","),
        ref_price, pct_change, momentum_signal, signal,
        buys, sells, rejected,
        total_orders_placed, total_orders_failed,
        total_fills, fill_volume, fill_rate,
        selected_markets, total_markets, depth_filtered,
        max_order,
        cfg.risk.max_total_exposure_usdc,
        cfg.risk.min_cash_buffer_usdc,
        realized_pnl, pnl_pct,
        config_valid,
    );

    if let Err(e) = std::fs::write("live_dashboard.json", &json) {
        tracing::warn!("failed to write live_dashboard.json: {}", e);
    }
}

/// Count fills and total volume from live_fills.jsonl
fn count_fills() -> (usize, f64) {
    let path = "live_fills.jsonl";
    let contents = match std::fs::read_to_string(path) {
        Ok(c) => c,
        Err(_) => return (0, 0.0),
    };
    let mut count = 0usize;
    let mut volume = 0.0f64;
    for line in contents.lines() {
        if line.trim().is_empty() { continue; }
        count += 1;
        // Quick parse for size_matched field
        if let Some(idx) = line.find("\"size_matched\":\"") {
            let rest = &line[idx + 16..];
            if let Some(end) = rest.find('"') {
                if let Ok(v) = rest[..end].parse::<f64>() {
                    volume += v;
                }
            }
        }
    }
    (count, volume)
}

/// Log a live fill event to live_fills.jsonl
fn log_live_fill(order_id: &str, quote: &paper::QuoteIntent, size_matched: &rust_decimal::Decimal) {
    let now = chrono::Utc::now().to_rfc3339();
    let line = format!(
        r#"{{"timestamp":"{}","event":"FILL","order_id":"{}","side":"{}","outcome":"{}","price":"{}","size_usdc":"{}","size_matched":"{}","reason":"{}"}}"#,
        now, order_id, quote.side, quote.outcome, quote.price, quote.size_usdc, size_matched, quote.reason
    );
    if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open("live_fills.jsonl") {
        let _ = writeln!(f, "{}", line);
    }
}

/// Log a live order event (resting/placed) to live_orders.jsonl
fn log_live_order(order_id: &str, quote: &paper::QuoteIntent, status: &str) {
    let now = chrono::Utc::now().to_rfc3339();
    let line = format!(
        r#"{{"timestamp":"{}","event":"{}","order_id":"{}","side":"{}","outcome":"{}","price":"{}","size_usdc":"{}"}}"#,
        now, status, order_id, quote.side, quote.outcome, quote.price, quote.size_usdc
    );
    if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open("live_orders.jsonl") {
        let _ = writeln!(f, "{}", line);
    }
}

/// Log a system event to live_events.jsonl
pub fn log_system_event(event_type: &str, message: &str) {
    let now = chrono::Utc::now().to_rfc3339();
    let json = format!(
        r#"{{"timestamp":"{}","type":"{}","message":"{}"}}"#,
        now, event_type, message.replace('"', "\\\"")
    );
    if let Ok(mut file) = std::fs::OpenOptions::new().create(true).append(true).open("live_events.jsonl") {
        use std::io::Write;
        let _ = writeln!(file, "{}", json);
    }
}

/// Log a market discovery to live_markets.jsonl
pub fn log_market_watch(market: &crate::models::GammaMarket, outcome: &str, bid: f64, ask: f64, pick: f64, status: &str) {
    let now = chrono::Utc::now().to_rfc3339();
    let title = market.question.replace('"', "\\\"");
    let json = format!(
        r#"{{"timestamp":"{}","title":"{}","outcome":"{}","bid":{},"ask":{},"selected_price":{},"status":"{}"}}"#,
        now, title, outcome, bid, ask, pick, status
    );
    if let Ok(mut file) = std::fs::OpenOptions::new().create(true).append(true).open("live_markets.jsonl") {
        use std::io::Write;
        let _ = writeln!(file, "{}", json);
    }
}