"""
Chain Gambler Live Dashboard — reads live_dashboard.json from the Rust bot.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.widgets import Footer, Header, Static

GREEN   = "#00ff99"
RED     = "#ff4d6d"
CYAN    = "#00d4ff"
YELLOW  = "#ffd166"
PURPLE  = "#9d5cff"
DIM     = "#445566"
WHITE   = "#ffffff"
GOLD    = "#ffcc00"

BLOCKS  = "▁▂▃▄▅▆▇█"


def cash(v: float) -> str:
    return f"${v:,.2f}"


def pct_str(v: float) -> str:
    return f"{v:+.2f}%"


def spark(values: list, width: int = 60) -> str:
    if len(values) < 2:
        return ""
    if len(values) > width:
        step = len(values) / width
        values = [values[int(i * step)] for i in range(width)]
    lo, hi = min(values), max(values)
    span = hi - lo
    if span < 0.0001:
        return f"[{GREEN}]{'▄' * len(values)}[/]"
    out = []
    for v in values:
        idx = int((v - lo) / span * (len(BLOCKS) - 1))
        out.append(f"[{GREEN}]{BLOCKS[idx]}[/]")
    return "".join(out)


def bar(v: float, width: int = 20, color: str = GREEN) -> str:
    v = max(0, min(1, v))
    n = int(v * width)
    return f"[{color}]{'█' * n}[/][{DIM}]{'░' * (width - n)}[/]"


def utc_ts() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


HISTORY_FILE = ".equity_history.log"


class Dashboard(App):
    TITLE = "Chain Gambler v0.3"
    SUB_TITLE = "Live Trading Dashboard"

    CSS = """
    Screen { background: #0a0e17; color: #b0c4de; }
    Header { background: #111827; color: #e0e8f0; }
    Footer { background: #111827; color: #556677; }
    .panel { background: #111827; border: round #1e3355; padding: 1 2; margin: 0 1 1 0; }
    .hero  { border: tall #00d4ff; background: #0d1520; padding: 1 2; margin: 0 1 1 0; }
    .warn  { border: round #ff4d6d; background: #150d12; padding: 1 2; margin: 0 1 1 0; }
    .profit-flash { border: tall #00ff99; background: #0a1a10; padding: 1 2; margin: 0 1 1 0; }
    #top   { height: 3; }
    #body  { height: 1fr; }
    #left  { width: 35%; }
    #mid   { width: 40%; }
    #right { width: 25%; }
    """

    def __init__(self):
        super().__init__()
        self._equity_hist: list[float] = []
        self._last_fills: int = 0
        self._last_pnl: float = 0.0
        self._profit_flash: int = 0  # countdown frames for profit animation
        self._load_history()

    def _load_history(self):
        if os.path.exists(HISTORY_FILE):
            try:
                with open(HISTORY_FILE) as f:
                    self._equity_hist = [float(l.strip()) for l in f if l.strip()]
            except Exception:
                self._equity_hist = []

    def _save_history(self):
        try:
            with open(HISTORY_FILE, "w") as f:
                for v in self._equity_hist[-200:]:
                    f.write(f"{v}\n")
        except Exception:
            pass

    def compose(self) -> ComposeResult:
        yield Header()
        with Container():
            yield Static(id="top", classes="panel")
            with Horizontal(id="body"):
                with Vertical(id="left"):
                    yield Static(id="equity", classes="hero")
                    yield Static(id="positions", classes="panel")
                with Vertical(id="mid"):
                    yield Static(id="signal", classes="panel")
                    yield Static(id="markets", classes="panel")
                with Vertical(id="right"):
                    yield Static(id="stats", classes="panel")
                    yield Static(id="risk", classes="warn")
        yield Footer()

    def on_mount(self):
        self.set_interval(1.0, self.tick)

    def tick(self):
        data = {}
        try:
            with open("live_dashboard.json") as f:
                data = json.load(f)
        except Exception:
            pass

        # Track equity history
        eq = float(data.get("equity", 0))
        if eq > 0:
            if not self._equity_hist or abs(eq - self._equity_hist[-1]) > 0.0001:
                self._equity_hist.append(eq)
                self._equity_hist = self._equity_hist[-200:]
                self._save_history()

        # Profit animation: flash when P&L increases
        pnl = float(data.get("realized_pnl", 0))
        n_fills = int(data.get("total_fills", 0))
        if n_fills > self._last_fills and pnl > self._last_pnl:
            self._profit_flash = 3  # flash for 3 ticks
        self._last_fills = n_fills
        self._last_pnl = pnl
        if self._profit_flash > 0:
            self._profit_flash -= 1

        fills = self._read_fills()

        self.query_one("#top").update(self._top(data))
        self.query_one("#equity").update(self._equity(data))
        self.query_one("#positions").update(self._positions(data))
        self.query_one("#signal").update(self._signal(data))
        self.query_one("#markets").update(self._markets(data))
        self.query_one("#stats").update(self._stats(data, fills))
        self.query_one("#risk").update(self._risk(data))

        # Swap equity border class for profit animation
        eq_panel = self.query_one("#equity")
        if self._profit_flash > 0:
            eq_panel.remove_class("hero")
            eq_panel.add_class("profit-flash")
        else:
            eq_panel.remove_class("profit-flash")
            eq_panel.add_class("hero")

    # ── panels ──────────────────────────────────────────────

    def _top(self, d: dict) -> str:
        live = d.get("signal", "HOLD")
        btc = d.get("btc_price", 0)
        mom = d.get("momentum", 0.5)
        pct = d.get("btc_change_pct", 0) * 100

        mc = GREEN if mom > 0.53 else (RED if mom < 0.47 else YELLOW)
        dir_text = "▲ LONG" if mom > 0.53 else ("▼ SHORT" if mom < 0.47 else "─ FLAT")
        loop = d.get("iteration", 0)

        return (
            f"[bold {CYAN}]CHAIN GAMBLER v0.3[/]   "
            f"[{DIM}]Loop {loop}[/]   "
            f"[{DIM}]BTC[/] [bold {WHITE}]{btc:,.0f}[/] [{mc}]{pct:+.2f}%[/]   "
            f"[{DIM}]Mom[/] [{mc}]{mom:.4f}[/]  "
            f"[bold {mc}]{dir_text}[/]   "
            f"[{DIM}]{utc_ts()}[/]"
        )

    def _equity(self, d: dict) -> str:
        eq = float(d.get("equity", 0))
        bal = float(d.get("clob_balance", 0))
        pnl = float(d.get("realized_pnl", 0))
        pnl_pct = float(d.get("pnl_pct", 0))
        pos_val = float(d.get("position_value", 0))

        pc = GREEN if pnl >= 0 else RED

        # Sparkline
        line = spark(self._equity_hist, 55)

        # Profit animation banner
        banner = ""
        if self._profit_flash > 0:
            banner = f"\n  [bold {GOLD}]💰 PROFIT! +{cash(pnl)}[/]"

        # High / Low from history
        hi = max(self._equity_hist) if self._equity_hist else eq
        lo = min(self._equity_hist) if self._equity_hist else eq

        return (
            f"[bold {CYAN}]EQUITY CURVE[/]{banner}\n\n"
            f"  [{DIM}]Balance[/]      [bold {WHITE}]{cash(bal)}[/]   "
            f"[{DIM}]Pos[/] [bold]{cash(pos_val)}[/]\n"
            f"  [{DIM}]Total Equity[/] [bold {WHITE}]{cash(eq)}[/]   "
            f"[{DIM}]P&L[/] [bold {pc}]{cash(pnl)} ({pct_str(pnl_pct)})[/]\n"
            f"  [{DIM}]High[/] [bold]{cash(hi)}[/]  [{DIM}]Low[/] [bold]{cash(lo)}[/]\n\n"
            f"  {line}\n"
        )

    def _positions(self, d: dict) -> str:
        positions = d.get("positions", [])
        out = [f"[bold {CYAN}]POSITIONS[/] ({len(positions)})"]
        if not positions:
            out.append(f"  [{DIM}]No open positions[/]")
        else:
            for p in positions:
                side_c = GREEN if p.get("outcome") == "YES" else RED
                out.append(
                    f"  [{side_c}]{p.get('outcome', '?')}[/]  "
                    f"{float(p.get('shares', 0)):.1f} sh  "
                    f"[{DIM}]cost[/] {cash(float(p.get('cost_usdc', 0)))}"
                )
        return "\n".join(out)

    def _signal(self, d: dict) -> str:
        btc = d.get("btc_price", 0)
        mom = d.get("momentum", 0.5)
        buys = d.get("loop_buys", 0)
        sells = d.get("loop_sells", 0)
        skips = d.get("loop_rejected", 0) + d.get("thin_depth_skipped", 0)

        return (
            f"[bold {CYAN}]SIGNAL[/]\n\n"
            f"  [{DIM}]BTC/USDT[/]  [bold]{btc:,.0f}[/]\n"
            f"  [{DIM}]Momentum[/]  [bold]{mom:.4f}[/]\n\n"
            f"  [bold {CYAN}]This Loop[/]\n"
            f"  [{GREEN}]▲ Buy[/]     {buys:<5}  [{RED}]▼ Sell[/]  {sells:<5}\n"
            f"  [{YELLOW}]✕ Skip[/]    {skips:<5}"
        )

    def _markets(self, d: dict) -> str:
        markets = d.get("market_logs", [])
        n = d.get("selected_markets", 0)
        total = d.get("total_markets", 0)
        thin = d.get("thin_depth_skipped", 0)
        out = [f"[bold {CYAN}]MARKETS[/]  ({n} active / {total} total / {thin} filtered)"]
        for m in markets[:6]:
            title = m.get("title", "?")[:40]
            price = m.get("price", 0)
            out.append(f"  [{DIM}]{price:.4f}[/]  {title}")
        return "\n".join(out)

    def _stats(self, d: dict, fills: list) -> str:
        placed = d.get("orders_placed", 0)
        failed = d.get("orders_failed", 0)
        n_fills = d.get("total_fills", 0)
        vol = d.get("fill_volume", 0)
        rate = d.get("fill_rate", 0)

        out = [
            f"[bold {CYAN}]ORDERS[/]",
            f"  Placed: [bold]{placed}[/]   Failed: [{RED}]{failed}[/]",
            f"  Fills:  [bold]{n_fills}[/]   Vol: [bold]{cash(vol)}[/]",
            f"  Fill Rate: [bold]{rate:.0f}%[/]\n",
            f"[bold {CYAN}]RECENT FILLS[/]",
        ]
        if not fills:
            out.append(f"  [{DIM}]No fills yet[/]")
        else:
            for f in fills[:6]:
                side_c = GREEN if f["side"] == "BUY" else RED
                out.append(
                    f"  {f['ts']}  [{side_c}]{f['side']:<4}[/] {f['outcome']:>3}  "
                    f"${f['price']:.2f}  {cash(f['filled'])}"
                )
        return "\n".join(out)

    def _risk(self, d: dict) -> str:
        max_exp = d.get("max_exposure_usdc", "13")
        max_ord = d.get("max_order_usdc", "5")
        buf = d.get("min_cash_buffer", "8")
        valid = d.get("config_valid", True)
        status = f"[{GREEN}]OK[/]" if valid else f"[{RED}]ISSUE[/]"
        return (
            f"[bold {RED}]RISK LIMITS[/]\n\n"
            f"  Max Exposure:  [bold]{cash(float(max_exp))}[/]\n"
            f"  Max Order:     [bold]{cash(float(max_ord))}[/]\n"
            f"  Cash Buffer:   [bold]{cash(float(buf))}[/]\n"
            f"  Config:        {status}"
        )

    def _read_fills(self) -> list:
        fills = []
        try:
            path = "live_fills.jsonl"
            if not os.path.exists(path):
                return fills
            with open(path) as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    row = json.loads(line)
                    usdc = float(row.get("size_usdc", 0) or 0)
                    price = float(row.get("price", 0) or 0)
                    ts = (row.get("timestamp", "") or "").split("T")[-1][:8]
                    fills.append(dict(
                        ts=ts,
                        side=row.get("side", ""),
                        outcome=row.get("outcome", ""),
                        price=price,
                        filled=usdc,
                    ))
        except Exception:
            pass
        return fills[::-1][:20]


if __name__ == "__main__":
    Dashboard().run()
