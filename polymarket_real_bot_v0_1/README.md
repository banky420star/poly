# Chain Gambler v0.3 — Polymarket Autonomous Trading Bot

A dual-engine Polymarket trading system combining a high-performance Rust core with a Python
AI/execution sidecar. Paper-first by design, with a fully integrated live EIP-712 execution
pipeline via the Polymarket V2 SDK.

> **⚠️ This is NOT a profit-guaranteeing bot.** It is production-grade *machinery* for
> autonomous algorithmic execution: market discovery, real-time WebSocket price feeds,
> order-book-aware quoting, directional momentum strategy, risk guardrails, Grok AI
> sentiment analysis, ML signal ensembles, and live order signing/placement.

---

## Architecture Overview

```
┌──────────────────────────────────────────────────────────┐
│                    Rust Core (tokio)                      │
│                                                          │
│  Binance WS ──► BinanceFeed ──► momentum_signal()        │
│                                       │                  │
│  Gamma REST ──► GammaClient ──► MarketSelector           │
│                                       │                  │
│  Poly WS ────► PolyWsFeed ──► MarketBookCache            │
│                                       │                  │
│  CLOB REST ──► ClobClient ──► OrderBookSummary           │
│                                       ▼                  │
│                            ReactiveDirectional           │
│                            (strategy engine)             │
│                                       │                  │
│                              RiskEngine.check()          │
│                                       │                  │
│                        ┌──────────────┼──────────────┐   │
│                        ▼              ▼              │   │
│                   PaperLedger    LiveExecutor         │   │
│                   (--paper)      (--live / SDK)       │   │
│                        │              │              │   │
│                  paper_state.json  EIP-712 signed     │   │
│                  paper_ledger.jsonl  orders → CLOB    │   │
└──────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────┐
│              Python Sidecar (FastAPI :4002)               │
│                                                          │
│  CLOBExecutor ──► limit/market orders via L2 auth        │
│  GrokAgent ─────► xAI Grok-3 market sentiment           │
│  MLSignalGenerator ► feature extraction + scoring        │
│  SignalEnsemble ──► ML × Grok combined decisions         │
└──────────────────────────────────────────────────────────┘
```

---

## What This Version Does

### Rust Core
- **Market Discovery** — Fetches active markets from the Gamma API, filters by configurable
  keyword include/exclude lists (crypto, BTC, ETH, SOL, etc.).
- **Real-Time Price Feeds** — Binance WebSocket ticker for BTC/USDT momentum; Polymarket
  WebSocket for live order book snapshots (Level 2, best bid/ask, price changes).
- **CLOB Order Book Client** — REST queries for per-token order books, midpoints, spread
  analysis, and depth aggregation (bid/ask USDC depth, level counts).
- **Reactive Directional Strategy** — Momentum-based directional loading near market expiry,
  plus passive market-making quotes. Fee-aware pricing (maker vs taker). Depth-aware position
  sizing that scales down in thin books.
- **Risk Engine** — Per-order size cap, total/per-market exposure limits, cash buffer floor,
  open intent ceiling.
- **Paper Ledger** — Persistent JSON state + append-only JSONL event log. Two fill modes:
  `record_only` (log intents) and `immediate` (simulate fills with position tracking).
- **Live Executor** — Full EIP-712 order signing via `polymarket_client_sdk_v2`. Authenticated
  with L2 API credentials. Limit order placement, cancel-all on Ctrl+C shutdown.
- **Terminal Dashboard** — Rich ANSI dashboard with equity, P&L, momentum, positions, and
  recent intents. Separate `dashboard` watch mode for monitoring from another terminal.

### Python Sidecar
- **FastAPI Execution Server** (`py_executor/server.py` on `:4002`) — REST bridge for CLOB
  order management, live/paper mode gating, and AI-powered analysis.
- **Grok AI Agent** — Calls xAI Grok-3 for per-market edge detection, batch analysis, and
  quick sentiment pulses.
- **ML Signal Generator** — Feature extraction (price, volume, time decay, arb signals,
  volatility proxy) with a scoring ensemble. Placeholder for a trained RL policy via
  Stable Baselines3.
- **Signal Ensemble** — Combines ML scores with Grok confidence-weighted directional signals
  for unified trade decisions.

---

## Quick Start

### Prerequisites

- Rust toolchain (1.70+)
- Python 3.10+ (for the sidecar, optional)

### Install & Build

```bash
# Install Rust (if needed)
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
source "$HOME/.cargo/env"

# Configure
cp config.example.toml config.toml
cp .env.example .env
# Edit .env with your keys (see Environment Variables below)

# Build
cargo build --release
```

### Run Paper Mode

```bash
cargo run --release -- run --config config.toml --paper
```

### Run Live Mode

```bash
cargo run --release -- run --config config.toml --live
```

> **CAUTION:** Live mode submits real EIP-712 signed orders to the Polymarket CLOB.
> Real USDC is at risk. Start with tiny `max_order_usdc` limits.

### Other Commands

```bash
# Watch dashboard (run in a separate terminal)
cargo run --release -- dashboard --config config.toml

# List selected markets with order book spreads
cargo run --release -- markets --config config.toml

# Show paper ledger state
cargo run --release -- status --config config.toml

# Show full order book depth for selected markets
cargo run --release -- depth --config config.toml
```

### Python Sidecar (Optional)

```bash
cd py_executor
pip install -r requirements.txt
python server.py
# Server starts on http://127.0.0.1:4002
```

**Endpoints:**
| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Server health + mode status |
| `GET` | `/clob/orders` | List open CLOB orders |
| `GET` | `/clob/trades` | List recent trades |
| `GET` | `/clob/book/{token_id}` | Order book for a token |
| `POST` | `/clob/order` | Place limit/market order (live mode only) |
| `DELETE` | `/clob/orders` | Cancel all open orders |
| `POST` | `/live/toggle?gate=CONFIRM_LIVE` | Enable live trading |
| `POST` | `/live/disable` | Disable live trading |
| `POST` | `/grok/analyze` | Single market AI analysis |
| `POST` | `/grok/batch-analyze` | Batch AI analysis |
| `POST` | `/grok/pulse?topic=...` | Quick sentiment pulse |
| `POST` | `/paper/trade` | Record a paper trade from the Rust bot |

---

## Environment Variables

Create a `.env` file (never commit it):

```env
# Wallet (32-byte private key, with or without 0x prefix)
PRIVATE_KEY=0x...
DEPOSIT_WALLET_ADDRESS=0x...

# Polymarket L2 API credentials
POLY_API_KEY=...
POLY_API_SECRET=...
POLY_PASSPHRASE=...

# Optional: xAI Grok API key (for Python sidecar)
GROK_API_KEY=...

# Logging
RUST_LOG=info,polymarket_real_bot=debug
```

---

## Configuration (`config.toml`)

```toml
gamma_base_url = "https://gamma-api.polymarket.com"
clob_base_url  = "https://clob.polymarket.com"

dry_run_iterations = 0    # 0 = run forever
loop_interval_ms   = 3000 # main loop cadence

[paper]
starting_cash_usdc = "10000"
ledger_state_path  = "paper_state.json"
ledger_events_path = "paper_ledger.jsonl"
fill_mode          = "record_only"   # or "immediate"

[risk]
max_total_exposure_usdc  = "2000"
max_market_exposure_usdc = "300"
max_order_usdc           = "100"
min_cash_buffer_usdc     = "150"
max_open_intents         = 80

[strategy]
name                             = "reactive_directional_v1"
min_yes_mid                      = "0.05"
max_yes_mid                      = "0.95"
maker_edge_bps                   = 120
quote_spread_bps                 = 250
allow_buy_yes                    = true
allow_buy_no                     = true
directional_load_window_seconds  = 90
min_skew_ratio                   = 2.5
reference_feed                   = "binance"

[fees]
maker_fee_bps  = 0
taker_fee_bps  = 80
prefer_maker   = true

[market_filter]
limit            = 150
require_active   = true
require_open     = true
keywords         = ["bitcoin", "btc", "ethereum", "eth", "solana", "crypto"]
exclude_keywords = ["sports", "election", "politics", "trump", "biden"]
min_depth_usdc   = "50"
```

---

## File Layout

```
src/
  main.rs        CLI entrypoint, dashboard rendering, bot loop orchestration
  config.rs      TOML + env config structs (AppConfig, RiskConfig, StrategyConfig, etc.)
  gamma.rs       Gamma REST API client (market discovery)
  models.rs      GammaMarket model, DepthData, JSON deserialization helpers
  selector.rs    Keyword-based market filtering
  strategy.rs    ReactiveDirectional strategy — momentum skew, depth-aware sizing, fee-adjusted pricing
  risk.rs        RiskEngine — exposure limits, cash buffer, intent cap
  paper.rs       PaperLedger — persistent state, event logging, simulated fills
  clob.rs        CLOB REST client — order books, spreads, midpoints, depth aggregation
  feed.rs        Binance WebSocket ticker — rolling price history, momentum signal
  poly_ws.rs     Polymarket WebSocket — Level 2 book updates, price changes, best bid/ask
  executor.rs    LiveExecutor — EIP-712 order signing via SDK, L2 auth, cancel-all
  live.rs        Live trading setup (thin wrapper over LiveExecutor::from_env)
  signing.rs     HMAC-SHA256 L2 request signing, API credential management

py_executor/
  server.py        FastAPI bridge server (:4002)
  clob_executor.py Direct CLOB REST client (Python, L2 auth)
  grok_agent.py    Grok-3 AI market analysis agent
  ml_signals.py    Feature extraction, ML scoring, Signal Ensemble
  requirements.txt Python dependencies

systemd/
  polymarket-real-bot.service   Systemd unit file for headless deployment

config.example.toml   Template config (safe defaults, dry_run_iterations=3)
.env.example          Template environment variables (dummy keys)
paper_state.json      Paper ledger state (auto-created)
paper_ledger.jsonl    Paper ledger event log (auto-created)
```

---

## Strategy: Reactive Directional v1

The strategy operates in two modes depending on market state:

1. **Directional Loading** — When a market is within `directional_load_window_seconds` of
   expiry and momentum skew exceeds `min_skew_ratio`, the bot takes an aggressive directional
   position (BUY YES if bullish, BUY NO if bearish). Size scales with skew confidence
   (4×–8× base size) and is further adjusted by order book depth.

2. **Passive Market-Making** — Outside the load window (or when skew is weak), the bot places
   maker-style quotes at the bid, adjusted for maker fees and desired edge. Suppressed
   during active directional loading to avoid self-crossing.

**Momentum Signal:** A 5-minute rolling window of BTC/USDT prices from Binance, mapped to a
0.1–0.9 probability (0.5 = neutral). The signal is cross-referenced with each market's YES
mid-price to compute directional skew.

**Depth Filtering:** Markets with ask-side depth below `min_depth_usdc` are skipped entirely.
For tradeable markets, order size is proportionally reduced when book depth is less than 3×
the intended order size.

---

## Safety & Kill Switch

- **Ctrl+C** triggers a graceful shutdown: cancels all open CLOB orders before exiting.
- **`dry_run_iterations`** — set to a small number (1–3) for smoke testing.
- **Risk Engine** enforces hard limits on every quote before submission.
- **Live executor** independently caps order size at `max_order_usdc`.
- **Python sidecar** has an explicit live/paper gate (`/live/toggle?gate=CONFIRM_LIVE`).

---

## Deployment (Headless)

A systemd service file is provided at `systemd/polymarket-real-bot.service`. Edit paths
and user, then:

```bash
sudo cp systemd/polymarket-real-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now polymarket-real-bot
sudo journalctl -u polymarket-real-bot -f
```

---

## Security Notes

- **Never commit `.env`** — it contains your private key and API credentials.
- **Never put private keys in code.**
- **Run with a dedicated wallet and limited funds.**
- **Start live mode with tiny limits** only after successful paper testing.
- The `.gitignore` excludes `.env`, `paper_state.json`, `paper_ledger.jsonl`, and `target/`.

---

## Recommended Path

1. Run paper mode (`--paper`) for several days. Review `paper_ledger.jsonl`.
2. Run `depth` command to verify order book connectivity and liquidity.
3. Start the Python sidecar and test Grok AI analysis on selected markets.
4. Set `dry_run_iterations = 1` and `max_order_usdc = "1"` for a single live iteration.
5. Monitor with `dashboard` in a separate terminal.
6. Gradually increase limits only after validating the full pipeline.

---

## License

MIT
