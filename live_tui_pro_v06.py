#!/usr/bin/env python3
"""
CHAIN GAMBLER v0.6 — Professional Live Terminal Dashboard

Reads:
  - live_dashboard.json   main bot state
  - live_fills.jsonl      fills feed
  - live_markets.jsonl    market watch feed
  - live_events.jsonl     risk/system/execution event feed
  - live_positions.jsonl  optional positions feed

Run:
  pip install rich
  python live_tui_pro_v06.py
"""

import json
import time
from pathlib import Path
from datetime import datetime, timezone

from rich.align import Align
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box


DASHBOARD_FILE = Path("live_dashboard.json")
FILLS_FILE = Path("live_fills.jsonl")
MARKETS_FILE = Path("live_markets.jsonl")
EVENTS_FILE = Path("live_events.jsonl")
POSITIONS_FILE = Path("live_positions.jsonl")

REFRESH_PER_SECOND = 4
MAX_MARKET_ROWS = 9
MAX_EVENT_ROWS = 8
MAX_FILL_ROWS = 7
MAX_POSITION_ROWS = 7

console = Console()


# ---------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------

def read_json(path: Path, fallback):
    if not path.exists():
        return fallback

    try:
        return json.loads(path.read_text())
    except Exception:
        return fallback


def load_jsonl(path: Path, limit=20):
    if not path.exists():
        return []

    rows = []
    try:
        for line in path.read_text().splitlines()[-limit:]:
            line = line.strip()
            if not line:
                continue

            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    except Exception:
        pass

    return rows[-limit:]


def load_dashboard():
    return read_json(DASHBOARD_FILE, {})


def load_fills(limit=MAX_FILL_ROWS):
    return load_jsonl(FILLS_FILE, limit=limit)


def load_markets(data, limit=MAX_MARKET_ROWS):
    rows = load_jsonl(MARKETS_FILE, limit=limit)
    if rows:
        return rows

    fallback = data.get("monitored_markets", [])
    if isinstance(fallback, list):
        return fallback[-limit:]

    return []


def load_events(data, limit=MAX_EVENT_ROWS):
    rows = load_jsonl(EVENTS_FILE, limit=limit)
    if rows:
        return rows

    synthetic = []

    last_error = str(data.get("last_error", "") or "")
    if last_error:
        synthetic.append({
            "timestamp": now_iso(),
            "type": "ERROR",
            "message": last_error,
        })

    exposure = fnum(data.get("position_value", data.get("positions_value", 0)))
    max_exposure = fnum(data.get("max_total_exposure_usdc", data.get("max_exposure_usdc", 0)))
    if max_exposure > 0 and exposure >= max_exposure:
        synthetic.append({
            "timestamp": now_iso(),
            "type": "RISK",
            "message": f"Entry blocked: exposure {money(exposure)} at max {money(max_exposure)}",
        })

    loop_rejected = inum(data.get("loop_rejected", 0))
    if loop_rejected > 0:
        synthetic.append({
            "timestamp": now_iso(),
            "type": "RISK",
            "message": f"{loop_rejected} quote(s) rejected this loop",
        })

    return synthetic[-limit:]


def load_positions(data, limit=MAX_POSITION_ROWS):
    rows = load_jsonl(POSITIONS_FILE, limit=limit)
    if rows:
        return rows[-limit:]

    fallback = data.get("open_positions_list", data.get("positions", []))
    if isinstance(fallback, list):
        return fallback[-limit:]

    return []


# ---------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------

def now_iso():
    return datetime.now(timezone.utc).isoformat()


def fnum(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


def inum(value, default=0):
    try:
        return int(float(value))
    except Exception:
        return default


def money(value):
    try:
        value = float(value)
        sign = "-" if value < 0 else ""
        return f"{sign}${abs(value):,.2f}"
    except Exception:
        return "$0.00"


def price(value):
    try:
        return f"${float(value):.4f}"
    except Exception:
        return "-"


def price2(value):
    try:
        return f"${float(value):.2f}"
    except Exception:
        return "-"


def pct(value, decimals=2):
    try:
        return f"{float(value):.{decimals}f}%"
    except Exception:
        return "0.00%"


def signed_pct(value, decimals=2):
    try:
        return f"{float(value):+.{decimals}f}%"
    except Exception:
        return "+0.00%"


def parse_time(raw):
    raw = str(raw or "-")

    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.strftime("%H:%M:%S")
    except Exception:
        pass

    return raw[-8:] if len(raw) >= 8 else raw


def trim(text, width):
    text = str(text)
    if len(text) <= width:
        return text

    if width <= 3:
        return text[:width]

    return text[: width - 3] + "..."


def success_color(value, good=50, warn=10):
    value = fnum(value)

    if value >= good:
        return "bright_green"
    if value >= warn:
        return "bright_yellow"
    return "bright_red"


def pnl_color(value):
    return "bright_green" if fnum(value) >= 0 else "bright_red"


def sparkbar(value, width=18, good=50, warn=10):
    value = max(0.0, min(100.0, fnum(value)))
    filled = int(round((value / 100) * width))
    empty = max(0, width - filled)
    color = success_color(value, good=good, warn=warn)

    return f"[{color}]" + ("█" * filled) + "[/]" + "[dim]" + ("░" * empty) + "[/]"


def exposure_bar(value, max_value, width=24):
    value = max(0.0, fnum(value))
    max_value = max(0.000001, fnum(max_value, 1.0))

    used = min(100.0, value / max_value * 100.0)
    filled = int(round((used / 100) * width))
    empty = max(0, width - filled)

    if used >= 95:
        color = "bright_red"
    elif used >= 75:
        color = "bright_yellow"
    else:
        color = "bright_green"

    return f"[{color}]" + ("█" * filled) + "[/]" + "[dim]" + ("░" * empty) + "[/]", used, color


def get_loop(data):
    return data.get("loop", data.get("iteration", "?"))


def get_balance(data):
    return fnum(data.get("clob_balance", data.get("usdc_balance", data.get("balance", 0))))


def get_positions_value(data):
    return fnum(data.get("position_value", data.get("positions_value", data.get("exposure", 0))))


def get_position_count(data):
    return inum(data.get("position_count", data.get("open_positions", 0)))


def get_equity(data):
    equity = data.get("equity")
    if equity is not None:
        return fnum(equity)

    return get_balance(data) + get_positions_value(data)


def get_pnl(data):
    return fnum(data.get("net_pnl", data.get("realized_pnl", data.get("pnl", 0))))


def get_pnl_pct(data):
    return fnum(data.get("net_pnl_pct", data.get("pnl_pct", 0)))


def get_orders_placed(data):
    return inum(data.get("orders_placed", data.get("total_orders_placed", 0)))


def get_orders_failed(data):
    return inum(data.get("orders_failed", data.get("total_orders_failed", 0)))


def get_success_rate(data):
    placed = get_orders_placed(data)
    failed = get_orders_failed(data)
    total = placed + failed
    return placed / total * 100.0 if total else 0.0


def get_max_exposure(data):
    return fnum(data.get("max_total_exposure_usdc", data.get("max_exposure_usdc", 25)))


def get_max_order(data):
    return fnum(data.get("max_order_usdc", data.get("max_order_size", 5)))


def get_min_clob(data):
    return fnum(data.get("min_clob_size", 5))


def config_is_valid(data):
    min_clob = get_min_clob(data)
    max_order = get_max_order(data)

    if max_order < min_clob:
        return False, f"max_order {max_order:.2f} < CLOB min {min_clob:.2f}"

    max_exposure = get_max_exposure(data)
    if max_exposure <= 0:
        return False, "max exposure missing"

    return True, "SYSTEM NOMINAL"


def risk_locked(data):
    exposure = get_positions_value(data)
    max_exposure = get_max_exposure(data)
    projected = fnum(data.get("projected_exposure_usdc", exposure))

    return max_exposure > 0 and (exposure >= max_exposure or projected > max_exposure)


# ---------------------------------------------------------------------
# Panels
# ---------------------------------------------------------------------

def make_header(data, tick):
    version = str(data.get("version", "0.6"))
    loop = get_loop(data)

    btc_symbol = str(data.get("btc_symbol", "BTC/USDT"))
    btc_price = fnum(data.get("btc_price", 0))
    btc_change = fnum(data.get("btc_change_pct", 0))
    momentum = fnum(data.get("momentum", 0))
    signal = str(data.get("signal", "HOLD")).upper()

    mode = str(data.get("mode", "") or "").upper()

    if not mode:
        if risk_locked(data):
            mode = "RISK LOCKED"
        elif signal in {"BUY", "SELL"}:
            mode = "SIGNAL ACTIVE"
        else:
            mode = "SCANNING"

    signal_style = {
        "BUY": "bright_green",
        "SELL": "bright_red",
        "HOLD": "bright_yellow",
        "WAIT": "bright_yellow",
        "RISK_LOCKED": "bright_red",
        "ERROR": "bright_red",
    }.get(signal, "white")

    mode_style = "bright_red" if "RISK" in mode or "ERROR" in mode else "cyan"
    change_style = "bright_green" if btc_change >= 0 else "bright_red"

    spinner = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    spin = spinner[tick % len(spinner)]

    now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

    title = Text()
    title.append("CHAIN GAMBLER ", style="bold white")
    title.append(f"v{version}", style="bold bright_blue")
    title.append("   ")
    title.append("REAL CLOB", style="bold bright_green")
    title.append("   ")
    title.append("LIVE", style="bold bright_red")
    title.append(f"   LOOP {loop}", style="bold white")
    title.append(f"   {now}", style="dim white")

    line = Text()
    line.append(f"{spin}  ", style="bright_cyan")
    line.append("MODE: ", style="white")
    line.append(mode, style=f"bold {mode_style}")
    line.append("   |   ", style="dim")
    line.append(f"{btc_symbol}: ", style="white")
    line.append(money(btc_price), style="bold white")
    line.append("  ")
    line.append(signed_pct(btc_change), style=f"bold {change_style}")
    line.append("   |   ", style="dim")
    line.append(f"MOMENTUM: {momentum:.3f}", style="white")
    line.append("   |   ", style="dim")
    line.append("SIGNAL: ", style="white")
    line.append(signal, style=f"bold {signal_style}")

    grid = Table.grid(expand=True)
    grid.add_row(Align.center(title))
    grid.add_row(Align.center(line))

    border = "bright_red" if risk_locked(data) else "bright_blue"

    return Panel(
        grid,
        box=box.DOUBLE,
        border_style=border,
        padding=(0, 1),
    )


def make_account(data):
    cash = get_balance(data)
    equity = get_equity(data)
    pos_value = get_positions_value(data)
    pos_count = get_position_count(data)
    pnl = get_pnl(data)
    pnl_pct = get_pnl_pct(data)
    pnl_style = pnl_color(pnl)

    table = Table.grid(expand=True)
    table.add_column(min_width=20)
    table.add_column(justify="right")

    table.add_row("Cash Balance", f"[bold bright_green]{money(cash)}[/]")
    table.add_row("Total Equity", f"[bold cyan]{money(equity)}[/]")
    table.add_row("Open Exposure", f"[bold white]{money(pos_value)}[/]  [dim]({pos_count} pos)[/]")
    table.add_row("P&L", f"[bold {pnl_style}]{money(pnl)}  {signed_pct(pnl_pct)}[/]")

    border = "bright_green" if pnl >= 0 else "bright_red"

    return Panel(
        table,
        title="[bold white]ACCOUNT[/]",
        box=box.SQUARE,
        border_style=border,
    )


def make_execution(data):
    placed = get_orders_placed(data)
    failed = get_orders_failed(data)
    sr = get_success_rate(data)

    fills = inum(data.get("total_fills", data.get("fills", 0)))
    resting = inum(data.get("resting", data.get("resting_orders", 0)))
    fill_rate = fnum(data.get("fill_rate", 0))

    sr_style = success_color(sr, good=50, warn=10)
    fr_style = success_color(fill_rate, good=60, warn=25)

    table = Table.grid(expand=True)
    table.add_column(min_width=18)
    table.add_column(justify="right")

    table.add_row("Orders Placed", f"[bold]{placed}[/]")
    table.add_row("Orders Failed", f"[bold {'bright_red' if failed else 'bright_green'}]{failed}[/]")
    table.add_row("Success Rate", f"{sparkbar(sr, width=12, good=50, warn=10)}  [bold {sr_style}]{sr:.1f}%[/]")
    table.add_row("Fill Rate", f"{sparkbar(fill_rate, width=12, good=60, warn=25)}  [bold {fr_style}]{fill_rate:.1f}%[/]")
    table.add_row("Fills / Resting", f"[bright_green]{fills}[/] / [cyan]{resting}[/]")

    return Panel(
        table,
        title="[bold white]EXECUTION[/]",
        box=box.SQUARE,
        border_style=sr_style,
    )


def make_risk_lock(data):
    exposure = get_positions_value(data)
    max_exposure = get_max_exposure(data)
    projected = fnum(data.get("projected_exposure_usdc", exposure))
    bar, used_pct, used_style = exposure_bar(exposure, max_exposure, width=22)

    locked = risk_locked(data)

    table = Table.grid(expand=True)
    table.add_column(min_width=20)
    table.add_column(justify="right")

    table.add_row("Exposure", f"[bold]{money(exposure)}[/] / {money(max_exposure)}")
    table.add_row("Used", f"{bar} [bold {used_style}]{used_pct:.1f}%[/]")
    table.add_row("Projected Next", money(projected))

    if locked:
        table.add_row("Entry Engine", "[bold bright_red]BLOCKED[/]")
        table.add_row("Mode", "[bold bright_yellow]EXITS ONLY[/]")
    else:
        table.add_row("Entry Engine", "[bold bright_green]ACTIVE[/]")
        table.add_row("Mode", "[bold bright_green]NORMAL[/]")

    border = "bright_red" if locked else used_style

    return Panel(
        table,
        title="[bold white]RISK LOCK[/]",
        box=box.SQUARE,
        border_style=border,
    )


def make_health(data):
    valid, reason = config_is_valid(data)

    min_clob = get_min_clob(data)
    max_order = get_max_order(data)
    daily_loss = fnum(data.get("daily_loss_limit_usdc", data.get("max_daily_loss_usdc", 0)))
    cooldown = inum(data.get("cooldown_after_reject_secs", data.get("cooldown_secs", 0)))
    kill_switch = bool(data.get("kill_switch_armed", True))

    table = Table.grid(expand=True)
    table.add_column(min_width=20)
    table.add_column(justify="right")

    table.add_row("Max Order", f"[cyan]{money(max_order)}[/]")
    table.add_row("CLOB Min", f"[dim]{money(min_clob)}[/]")
    table.add_row("Daily Loss Limit", money(daily_loss))
    table.add_row("Reject Cooldown", f"{cooldown}s")
    table.add_row("Kill Switch", "[bold bright_green]ARMED[/]" if kill_switch else "[bold bright_red]OFF[/]")

    if valid:
        table.add_row("Config", "[bold bright_green]PASS[/]")
    else:
        table.add_row("Config", "[bold bright_red]FAIL[/]")
        table.add_row("Reason", f"[bright_red]{trim(reason, 34)}[/]")

    return Panel(
        table,
        title="[bold white]SYSTEM HEALTH[/]",
        box=box.SQUARE,
        border_style="bright_green" if valid else "bright_red",
    )


def make_scanner(data):
    selected = inum(data.get("selected_markets", data.get("markets_selected", 0)))
    total = inum(data.get("total_markets", data.get("markets_total", 0)))
    thin = inum(data.get("thin_depth_skipped", 0))

    loop_buys = inum(data.get("loop_buys", 0))
    loop_sells = inum(data.get("loop_sells", 0))
    loop_rej = inum(data.get("loop_rejected", 0))

    table = Table.grid(expand=True)
    table.add_column(min_width=20)
    table.add_column(justify="right")

    table.add_row("Markets", f"[bold cyan]{selected}[/] [dim]/ {total}[/]")
    table.add_row("Thin Depth Skipped", str(thin))
    table.add_row("Loop Buys", f"[bright_green]▲ {loop_buys}[/]")
    table.add_row("Loop Sells", f"[bright_red]▼ {loop_sells}[/]")
    table.add_row("Loop Rejected", f"[bold {'bright_red' if loop_rej else 'bright_green'}]{loop_rej}[/]")

    border = "bright_red" if loop_rej else "blue"

    return Panel(
        table,
        title="[bold white]SCANNER[/]",
        box=box.SQUARE,
        border_style=border,
    )


def make_market_watch(markets):
    table = Table(
        expand=True,
        box=box.SIMPLE_HEAD,
        show_header=True,
        header_style="bold cyan",
    )

    table.add_column("TIME", width=9)
    table.add_column("MARKET", overflow="fold")
    table.add_column("OUT", width=5)
    table.add_column("BID", justify="right", width=8)
    table.add_column("ASK", justify="right", width=8)
    table.add_column("PICK", justify="right", width=8)
    table.add_column("STATUS", width=12)

    if not markets:
        table.add_row(
            "-",
            "[dim]Waiting for live_markets.jsonl...[/]",
            "-",
            "-",
            "-",
            "-",
            "[dim]IDLE[/]",
        )
    else:
        for market in reversed(markets[-MAX_MARKET_ROWS:]):
            ts = parse_time(market.get("timestamp", market.get("time", "-")))

            slug = str(
                market.get("slug")
                or market.get("market")
                or market.get("question")
                or market.get("name")
                or "unknown-market"
            )

            outcome = str(market.get("outcome", market.get("side", "-"))).upper()
            bid = market.get("bid", market.get("best_bid", "-"))
            ask = market.get("ask", market.get("best_ask", "-"))
            pick = market.get("selected_price", market.get("target_price", market.get("price", "-")))
            status = str(market.get("status", "WATCHING")).upper()

            if status in {"SELECTED", "BUY", "ENTRY", "ACTIVE"}:
                status_style = "bright_green"
            elif status in {"SKIPPED", "REJECTED", "FAULT", "BLOCKED"}:
                status_style = "bright_red"
            elif status in {"WAIT", "WATCHING", "SCANNING"}:
                status_style = "bright_yellow"
            else:
                status_style = "white"

            table.add_row(
                ts,
                trim(slug, 46),
                outcome,
                price2(bid),
                price2(ask),
                f"[bold cyan]{price2(pick)}[/]",
                f"[bold {status_style}]{status}[/]",
            )

    return Panel(
        table,
        title="[bold white]MARKET WATCH LOG[/][dim]  monitored markets[/]",
        box=box.SQUARE,
        border_style="blue",
    )


def make_price_selector(data, tick):
    market = str(data.get("selected_market", data.get("current_market", "no active market")))
    outcome = str(data.get("selected_outcome", data.get("outcome", "-"))).upper()

    bid = fnum(data.get("best_bid", data.get("bid", 0.0)))
    ask = fnum(data.get("best_ask", data.get("ask", 1.0)))
    selected = fnum(data.get("selected_price", data.get("target_price", 0.5)))
    confidence = fnum(data.get("selection_confidence", data.get("confidence", 0.0)))

    min_price = 0.0
    max_price = 1.0
    width = 32

    def idx(value):
        value = max(min_price, min(max_price, fnum(value)))
        return int(round((value - min_price) / (max_price - min_price) * (width - 1)))

    bid_i = idx(bid)
    ask_i = idx(ask)
    selected_i = idx(selected)
    pulse_i = tick % width

    cells = []
    for i in range(width):
        if i == selected_i:
            cells.append("[bold bright_cyan]█[/]" if tick % 2 == 0 else "[bold bright_cyan]▓[/]")
        elif i == bid_i:
            cells.append("[bright_green]▌[/]")
        elif i == ask_i:
            cells.append("[bright_red]▐[/]")
        elif i == pulse_i:
            cells.append("[dim cyan]▒[/]")
        elif min(bid_i, ask_i) < i < max(bid_i, ask_i):
            cells.append("[dim]▓[/]")
        else:
            cells.append("[dim]░[/]")

    bar = "".join(cells)

    if confidence >= 0.70:
        conf_style = "bright_green"
    elif confidence >= 0.45:
        conf_style = "bright_yellow"
    else:
        conf_style = "bright_red"

    table = Table.grid(expand=True)
    table.add_column()
    table.add_column(justify="right")

    table.add_row("Market", f"[dim]{trim(market, 36)}[/]")
    table.add_row("Outcome", f"[bold white]{outcome}[/]")
    table.add_row("Bid / Ask", f"[bright_green]{price2(bid)}[/] / [bright_red]{price2(ask)}[/]")
    table.add_row("Selected", f"[bold bright_cyan]{price2(selected)}[/]")
    table.add_row("Confidence", f"[bold {conf_style}]{confidence:.2%}[/]")
    table.add_row("", "")
    table.add_row("[dim]0.00[/] " + bar + " [dim]1.00[/]", "")

    return Panel(
        table,
        title="[bold white]PRICE SELECTION[/][dim]  bid | ask | pick[/]",
        box=box.SQUARE,
        border_style="cyan",
    )


def make_system_log(events):
    table = Table(
        expand=True,
        box=box.SIMPLE_HEAD,
        show_header=True,
        header_style="bold cyan",
    )

    table.add_column("TIME", width=9)
    table.add_column("TYPE", width=9)
    table.add_column("MESSAGE", overflow="fold")

    if not events:
        table.add_row("-", "IDLE", "[dim]Waiting for live_events.jsonl...[/]")
    else:
        for event in reversed(events[-MAX_EVENT_ROWS:]):
            ts = parse_time(event.get("timestamp", event.get("time", "-")))
            event_type = str(event.get("type", "INFO")).upper()
            message = str(event.get("message", ""))

            if event_type in {"RISK", "ERROR", "EXIT"}:
                style = "bright_red"
            elif event_type in {"WARN", "CLOB"}:
                style = "bright_yellow"
            elif event_type in {"MARKET", "DISCOVERY"}:
                style = "cyan"
            elif event_type in {"FILL", "EXEC"}:
                style = "bright_green"
            else:
                style = "white"

            table.add_row(
                ts,
                f"[bold {style}]{event_type}[/]",
                trim(message, 96),
            )

    return Panel(
        table,
        title="[bold white]LIVE SYSTEM LOG[/][dim]  risk | market | exits | CLOB[/]",
        box=box.SQUARE,
        border_style="bright_blue",
    )


def make_positions(positions, data):
    table = Table(
        expand=True,
        box=box.SIMPLE_HEAD,
        show_header=True,
        header_style="bold cyan",
    )

    table.add_column("OUT", width=5)
    table.add_column("SHARES", justify="right", width=10)
    table.add_column("ENTRY", justify="right", width=10)
    table.add_column("BID", justify="right", width=10)
    table.add_column("COST", justify="right", width=10)
    table.add_column("P&L", justify="right", width=10)
    table.add_column("STATUS", overflow="fold")

    if not positions:
        count = get_position_count(data)
        value = get_positions_value(data)

        if count > 0:
            table.add_row(
                "-",
                "-",
                "-",
                "-",
                money(value),
                "-",
                "[dim]Position summary exists, but no live_positions.jsonl rows yet[/]",
            )
        else:
            table.add_row("-", "-", "-", "-", "-", "-", "[dim]No open positions[/]")
    else:
        for pos in reversed(positions[-MAX_POSITION_ROWS:]):
            outcome = str(pos.get("outcome", pos.get("side", "-"))).upper()
            shares = fnum(pos.get("shares", pos.get("size", pos.get("qty", 0))))
            entry = pos.get("entry_price", pos.get("entry", pos.get("price", "-")))
            bid = pos.get("best_bid", pos.get("bid", "-"))
            cost = fnum(pos.get("cost", pos.get("notional", 0)))
            pnl = fnum(pos.get("pnl", pos.get("unrealized_pnl", 0)))
            status = str(pos.get("status", "OPEN")).upper()

            out_style = "bright_green" if outcome == "YES" else "bright_red"
            pnl_style = pnl_color(pnl)

            table.add_row(
                f"[bold {out_style}]{outcome}[/]",
                f"{shares:,.2f}",
                price(entry),
                price(bid),
                money(cost),
                f"[bold {pnl_style}]{money(pnl)}[/]",
                status,
            )

    return Panel(
        table,
        title="[bold white]OPEN POSITIONS[/]",
        box=box.SQUARE,
        border_style="bright_yellow" if get_position_count(data) else "dim",
    )


def make_fills(fills, data):
    total_fills = inum(data.get("total_fills", data.get("fills", len(fills))))
    fill_volume = fnum(data.get("fill_volume", 0))

    if fill_volume == 0 and fills:
        fill_volume = sum(
            fnum(fill.get("size_matched", fill.get("filled", fill.get("size", 0))))
            for fill in fills
        )

    title_extra = f"  |  TOTAL {total_fills}  |  VOL {money(fill_volume)}" if total_fills else ""

    table = Table(
        expand=True,
        box=box.SIMPLE_HEAD,
        show_header=True,
        header_style="bold cyan",
    )

    table.add_column("TIME", width=9)
    table.add_column("SIDE", width=6)
    table.add_column("OUT", width=5)
    table.add_column("PRICE", justify="right", width=10)
    table.add_column("FILLED", justify="right", width=10)
    table.add_column("ORDER ID", overflow="fold")

    if not fills:
        table.add_row("-", "-", "-", "-", "-", "[dim]No fills in live_fills.jsonl yet[/]")
    else:
        for fill in reversed(fills[-MAX_FILL_ROWS:]):
            side = str(fill.get("side", "BUY")).upper()
            side_style = "bright_green" if side == "BUY" else "bright_red"

            ts = parse_time(fill.get("timestamp", fill.get("time", "-")))
            outcome = str(fill.get("outcome", fill.get("out", "-"))).upper()
            fill_price = fill.get("price", "-")
            matched = fill.get("size_matched", fill.get("filled", fill.get("size", 0)))
            order_id = trim(str(fill.get("order_id", fill.get("id", "-"))), 28)

            table.add_row(
                ts,
                f"[bold {side_style}]{side}[/]",
                outcome,
                price(fill_price),
                money(matched),
                f"[dim]{order_id}[/]",
            )

    return Panel(
        table,
        title=f"[bold white]LIVE FILLS[/][dim]{title_extra}[/]",
        box=box.SQUARE,
        border_style="cyan",
    )


def make_footer(data):
    valid, reason = config_is_valid(data)
    locked = risk_locked(data)

    footer = Text()
    footer.append("Ctrl+C to exit", style="dim")
    footer.append("  |  ")
    footer.append("Files: dashboard + fills + markets + events + positions", style="cyan")
    footer.append("  |  ")

    if not valid:
        footer.append(f"CONFIG BLOCKED: {reason}", style="bold bright_red")
    elif locked:
        footer.append("RISK LOCKED: entries blocked, exits only", style="bold bright_yellow")
    else:
        footer.append("SYSTEM NOMINAL", style="bold bright_green")

    return Panel(
        Align.center(footer),
        box=box.SIMPLE,
        border_style="dim",
    )


# ---------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------

def build_layout(tick):
    data = load_dashboard()
    fills = load_fills()
    markets = load_markets(data)
    events = load_events(data)
    positions = load_positions(data)

    layout = Layout()

    layout.split_column(
        Layout(name="header", size=5),
        Layout(name="metrics", size=7),
        Layout(name="risk", size=7),
        Layout(name="market_monitor", size=13),
        Layout(name="system_log", size=10),
        Layout(name="positions_and_fills", ratio=1),
        Layout(name="footer", size=3),
    )

    layout["metrics"].split_row(
        Layout(make_account(data)),
        Layout(make_execution(data)),
    )

    layout["risk"].split_row(
        Layout(make_health(data)),
        Layout(make_risk_lock(data)),
        Layout(make_scanner(data)),
    )

    layout["market_monitor"].split_row(
        Layout(make_market_watch(markets), ratio=2),
        Layout(make_price_selector(data, tick), ratio=1),
    )

    layout["positions_and_fills"].split_row(
        Layout(make_positions(positions, data)),
        Layout(make_fills(fills, data)),
    )

    layout["header"].update(make_header(data, tick))
    layout["system_log"].update(make_system_log(events))
    layout["footer"].update(make_footer(data))

    return layout


def main():
    console.print("[bold cyan]Initializing CHAIN GAMBLER v0.6 terminal dashboard...[/]")

    if not DASHBOARD_FILE.exists():
        console.print("[yellow]Waiting for live_dashboard.json...[/]")

    while not DASHBOARD_FILE.exists():
        time.sleep(1)

    tick = 0

    with Live(
        build_layout(tick),
        refresh_per_second=REFRESH_PER_SECOND,
        screen=True,
        console=console,
    ) as live:
        while True:
            live.update(build_layout(tick))
            tick += 1
            time.sleep(1 / REFRESH_PER_SECOND)


if __name__ == "__main__":
    main()
