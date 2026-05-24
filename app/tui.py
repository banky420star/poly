import json
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rich.align import Align
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.progress import BarColumn, Progress, TextColumn
from rich.table import Table
from rich.text import Text


LOG_PATH = Path("logs/snapshots.jsonl")
WS_URL = "ws://localhost:9876"


PIPELINE_STAGES = [
    ("DISCOVERY", "Find market"),
    ("ORDERBOOK", "Polymarket book"),
    ("PRICE_FEED", "Crypto spot"),
    ("FEATURES", "Build features"),
    ("PREDICTION", "Fair probability"),
    ("ODDS", "Value / edge"),
    ("ENTRY", "Entry decision"),
    ("POSITION", "Open position"),
    ("EXIT_FLIP", "Exit / flip"),
    ("RECORDER", "Store logs"),
]

EVENT_TO_STAGE = {
    "market_discovery": "DISCOVERY",
    "orderbook": "ORDERBOOK",
    "orderbook_update": "ORDERBOOK",
    "price": "PRICE_FEED",
    "price_update": "PRICE_FEED",
    "features": "FEATURES",
    "prediction": "PREDICTION",
    "odds": "ODDS",
    "decision": "ENTRY",
    "paper_entry": "POSITION",
    "position": "POSITION",
    "exit_check": "EXIT_FLIP",
    "paper_exit": "EXIT_FLIP",
    "closed_trade": "EXIT_FLIP",
    "loop_state": "RECORDER",
}

# TTL thresholds for stage status
ACTIVE_TTL_SEC = 2.0
RECENT_TTL_SEC = 10.0


# Shared state between WS thread and render loop
_events: deque = deque(maxlen=1000)
_ws_connected = False
_lock = threading.Lock()

# Track per-stage last-seen timestamps
_stage_ts: dict[str, datetime] = {}


def _ws_reader():
    global _ws_connected
    try:
        from websockets.sync.client import connect as ws_connect

        while True:
            try:
                with ws_connect(WS_URL, open_timeout=3) as ws:
                    with _lock:
                        _ws_connected = True
                    for line in ws:
                        try:
                            evt = json.loads(line)
                            with _lock:
                                _events.append(evt)
                        except json.JSONDecodeError:
                            pass
            except Exception:
                with _lock:
                    _ws_connected = False
                time.sleep(2)
    except ImportError:
        pass


def _file_reader() -> list[dict]:
    if not LOG_PATH.exists():
        return []
    try:
        lines = LOG_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    events: list[dict] = []
    for line in lines[-500:]:
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def read_events() -> tuple[list[dict], bool]:
    with _lock:
        if _events:
            return list(_events), _ws_connected
    return _file_reader(), False


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def fmt(value: Any, digits: int = 2) -> str:
    try:
        if value is None:
            return "-"
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "-"


def pct(value: Any) -> str:
    try:
        if value is None:
            return "-"
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return "-"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(ts_str: str | None) -> datetime | None:
    if not ts_str:
        return None
    try:
        s = ts_str.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def latest_market(events: list[dict]) -> dict:
    markets: dict[str, dict] = {}

    for event in events:
        market_id = event.get("market_id")
        if not market_id:
            continue

        row = markets.setdefault(
            market_id,
            {
                "market_id": market_id,
                "market": market_id,
                "symbol": None,
                "spot": None,
                "price_to_beat": None,
                "distance_to_target": None,
                "seconds_remaining": None,
                "prob_up": None,
                "prob_down": None,
                "confidence": None,
                "best_side": None,
                "market_ask": None,
                "adjusted_edge": None,
                "max_entry_price": None,
                "decision": None,
                "position_side": None,
                "avg_entry": None,
                "shares": None,
                "reason": [],
                "last_event": None,
                "last_ts": None,
            },
        )

        for key in row.keys():
            if key in event:
                row[key] = event[key]

        row["last_event"] = event.get("event", row.get("last_event"))
        row["last_ts"] = event.get("ts", row.get("last_ts"))

    if not markets:
        return {}

    return sorted(
        markets.values(),
        key=lambda m: safe_float(m.get("adjusted_edge"), -999),
        reverse=True,
    )[0]


def best_rejected(events: list[dict]) -> dict | None:
    """Find the best market that was NOT traded (NO_TRADE decision)."""
    best = None
    best_edge = -999.0

    for event in events:
        decision = event.get("decision", "")
        edge = safe_float(event.get("adjusted_edge"), -999)
        if decision == "NO_TRADE" and edge > best_edge:
            # Check this is from a recent decision event
            if event.get("event") == "decision":
                best = event
                best_edge = edge

    return best


def infer_stage_status(events: list[dict]) -> dict[str, tuple[str, str]]:
    """TTL-based stage status. Returns {stage: (status, icon)}."""
    now = _now()
    stage_ages: dict[str, float] = {}

    # Scan events to find latest timestamp per stage
    for event in events:
        event_name = str(event.get("event", ""))
        stage = event.get("stage") or EVENT_TO_STAGE.get(event_name)
        if not stage:
            continue
        ts = _parse_ts(event.get("ts"))
        if ts:
            age = (now - ts).total_seconds()
            if stage not in stage_ages or age < stage_ages[stage]:
                stage_ages[stage] = age

    status: dict[str, tuple[str, str]] = {}
    for stage_key, _label in PIPELINE_STAGES:
        age = stage_ages.get(stage_key)
        if age is None:
            status[stage_key] = ("idle", "·")
        elif age < ACTIVE_TTL_SEC:
            status[stage_key] = ("active", "▶")
        elif age < RECENT_TTL_SEC:
            status[stage_key] = ("recent", "✓")
        else:
            status[stage_key] = ("idle", "·")

    return status


def get_startup_state(events: list[dict]) -> dict:
    """Extract feed/mode state from loop_state events."""
    state = {
        "loop": 0,
        "mode": "paper",
        "binance_ws": False,
        "poly_ws": False,
        "markets": 0,
        "positions": 0,
        "live_mode": False,
    }
    for e in events:
        if e.get("event") == "loop_state":
            for k in state:
                if k in e:
                    state[k] = e[k]
    return state


def make_header(events: list[dict], ws_ok: bool) -> Panel:
    last_ts = events[-1].get("ts", "-") if events else "-"
    title = Text("POLY ODDS BOT TUI", style="bold cyan")
    conn = Text(" WS" if ws_ok else " file", style="green" if ws_ok else "dim")
    subtitle = Text.assemble(
        ("Live terminal cockpit | last: ", "dim"),
        (last_ts[-15:], "white"),
        conn,
    )

    content = Text()
    content.append(title)
    content.append("\n")
    content.append(subtitle)

    return Panel(Align.center(content), border_style="cyan", title="Control Room")


def make_pipeline_panel(events: list[dict]) -> Panel:
    status = infer_stage_status(events)

    table = Table.grid(expand=True)
    table.add_column(ratio=1)
    table.add_column(ratio=2)
    table.add_column(ratio=1, justify="right")

    style_map = {
        "active": "bold yellow",
        "recent": "green",
        "idle": "dim",
    }

    for stage, label in PIPELINE_STAGES:
        state, icon = status.get(stage, ("idle", "·"))
        style = style_map.get(state, "dim")
        state_label = state.upper() if state != "idle" else ""
        table.add_row(
            Text(f"{icon} {stage}", style=style),
            Text(label, style=style),
            Text(state_label, style=f"{style} italic"),
        )

    return Panel(table, title="Pipeline Stages", border_style="blue")


def make_startup_panel(events: list[dict]) -> Panel:
    s = get_startup_state(events)

    table = Table.grid(expand=True)
    table.add_column(ratio=1)
    table.add_column(ratio=1, justify="right")

    binance_icon = "green" if s["binance_ws"] else "yellow"
    poly_icon = "green" if s["poly_ws"] else "yellow"
    mode_icon = "red bold" if s["live_mode"] else "green"

    table.add_row(Text("Binance WS", style="dim"), Text("connected" if s["binance_ws"] else "warming up", style=binance_icon))
    table.add_row(Text("Poly WS", style="dim"), Text("connected" if s["poly_ws"] else "warming up", style=poly_icon))
    table.add_row(Text("Markets", style="dim"), Text(str(s["markets"]), style="white"))
    table.add_row(Text("Positions", style="dim"), Text(str(s["positions"]), style="white"))
    table.add_row(Text("Mode", style="dim"), Text("LIVE" if s["live_mode"] else "PAPER", style=mode_icon))

    return Panel(table, title="Startup Status", border_style="yellow")


def make_market_panel(market: dict) -> Panel:
    if not market:
        return Panel(
            Text("Waiting for bot snapshots...", style="dim"),
            title="Market",
            border_style="yellow",
        )

    table = Table.grid(expand=True)
    table.add_column(justify="left", ratio=1)
    table.add_column(justify="right", ratio=1)

    edge = safe_float(market.get("adjusted_edge"))
    edge_style = "green" if edge >= 0 else "red"

    rows = [
        ("Market", market.get("market", market.get("market_id"))[:50]),
        ("Symbol", market.get("symbol", "-")),
        ("Spot", fmt(market.get("spot"), 2)),
        ("Price to beat", fmt(market.get("price_to_beat"), 2)),
        ("Seconds left", fmt(market.get("seconds_remaining"), 0)),
        ("Best side", market.get("best_side", "-")),
        ("Market ask", fmt(market.get("market_ask"), 3)),
        ("Adjusted edge", pct(market.get("adjusted_edge"))),
        ("Decision", market.get("decision", "-")),
    ]

    for label, value in rows:
        style = edge_style if label == "Adjusted edge" else "white"
        table.add_row(Text(label, style="dim"), Text(str(value), style=style))

    return Panel(
        table,
        title="Best Market Snapshot",
        border_style="green" if edge >= 0 else "red",
    )


def make_probability_panel(market: dict) -> Panel:
    if not market:
        return Panel(
            Text("No probability data yet", style="dim"),
            title="Probability",
            border_style="magenta",
        )

    prob_up = max(0, min(1, safe_float(market.get("prob_up"))))
    prob_down = max(0, min(1, safe_float(market.get("prob_down"))))
    confidence = max(0, min(1, safe_float(market.get("confidence"))))

    progress = Progress(
        TextColumn("[bold]UP[/bold]"),
        BarColumn(bar_width=None),
        TextColumn("{task.percentage:>5.1f}%"),
        expand=True,
    )
    progress.add_task("UP", total=100, completed=prob_up * 100)

    progress_down = Progress(
        TextColumn("[bold]DOWN[/bold]"),
        BarColumn(bar_width=None),
        TextColumn("{task.percentage:>5.1f}%"),
        expand=True,
    )
    progress_down.add_task("DOWN", total=100, completed=prob_down * 100)

    progress_conf = Progress(
        TextColumn("[bold]CONF[/bold]"),
        BarColumn(bar_width=None),
        TextColumn("{task.percentage:>5.1f}%"),
        expand=True,
    )
    progress_conf.add_task("CONF", total=100, completed=confidence * 100)

    grid = Table.grid(expand=True)
    grid.add_row(progress)
    grid.add_row(progress_down)
    grid.add_row(progress_conf)

    return Panel(grid, title="Probability Engine", border_style="magenta")


def make_no_edge_panel(rejected: dict | None) -> Panel:
    if not rejected:
        return Panel(
            Text("", style="dim"),
            title="Best Rejected Setup",
            border_style="dim",
        )

    fair_up = safe_float(rejected.get("prob_up"), 0.5)
    ask = safe_float(rejected.get("market_ask"), 0.5)
    edge = safe_float(rejected.get("adjusted_edge"), 0)

    rows = [
        ("Market", str(rejected.get("market_id", "-"))[:40]),
        ("Symbol", str(rejected.get("symbol", "-"))),
        ("Side", str(rejected.get("best_side", "-"))),
        ("Fair", f"{fair_up:.3f}"),
        ("Ask", f"{ask:.3f}"),
        ("Edge", f"{edge:+.4f}"),
    ]

    table = Table.grid(expand=True)
    table.add_column(ratio=1)
    table.add_column(justify="right", ratio=1)

    for label, value in rows:
        style = "red" if label == "Edge" and edge < 0 else "white"
        table.add_row(Text(label, style="dim"), Text(value, style=style))

    return Panel(table, title="Best Rejected Setup", border_style="red")


def make_position_panel(market: dict) -> Panel:
    if not market or not market.get("position_side"):
        return Panel(
            Text("No open paper position", style="dim"),
            title="Position",
            border_style="yellow",
        )

    table = Table.grid(expand=True)
    table.add_column(ratio=1)
    table.add_column(justify="right", ratio=1)

    table.add_row("Side", str(market.get("position_side")))
    table.add_row("Avg entry", fmt(market.get("avg_entry"), 3))
    table.add_row("Shares", fmt(market.get("shares"), 3))

    return Panel(table, title="Paper Position", border_style="yellow")


def make_events_panel(events: list[dict]) -> Panel:
    table = Table(expand=True)
    table.add_column("Time", style="dim", no_wrap=True)
    table.add_column("Event", style="cyan", no_wrap=True)
    table.add_column("Market", style="white")
    table.add_column("Decision / Action", style="yellow")
    table.add_column("Reason", style="dim")

    for event in list(reversed(events[-12:])):
        reason = event.get("reason", "")
        if isinstance(reason, list):
            reason = ", ".join(str(x) for x in reason)

        table.add_row(
            str(event.get("ts", "-"))[-15:],
            str(event.get("event", "-")),
            str(event.get("market_id", "-")),
            str(event.get("decision") or event.get("action") or event.get("status") or "-"),
            str(reason)[:80],
        )

    return Panel(table, title="Latest Events", border_style="white")


def build_layout(events: list[dict], ws_ok: bool) -> Layout:
    market = latest_market(events)
    rejected = best_rejected(events)

    layout = Layout()
    layout.split_column(
        Layout(name="header", size=4),
        Layout(name="body", ratio=5),
        Layout(name="bottom", ratio=3),
    )

    layout["body"].split_row(
        Layout(name="left", ratio=1),
        Layout(name="mid", ratio=1),
        Layout(name="right", ratio=2),
    )

    layout["left"].split_column(
        Layout(name="pipeline", ratio=3),
        Layout(name="startup", ratio=2),
    )

    layout["mid"].split_column(
        Layout(name="probability"),
        Layout(name="position"),
    )

    layout["right"].split_column(
        Layout(name="market"),
        Layout(name="lower_right"),
    )

    no_trade = not market or not market.get("decision") or market.get("decision") == "NO_TRADE"

    layout["header"].update(make_header(events, ws_ok))
    layout["pipeline"].update(make_pipeline_panel(events))
    layout["startup"].update(make_startup_panel(events))
    layout["market"].update(make_market_panel(market))
    layout["probability"].update(make_probability_panel(market))
    layout["position"].update(make_position_panel(market))
    layout["lower_right"].update(make_no_edge_panel(rejected) if no_trade else make_position_panel(market))
    layout["bottom"].update(make_events_panel(events))

    return layout


def main():
    ws_thread = threading.Thread(target=_ws_reader, daemon=True)
    ws_thread.start()

    console = Console()

    with Live(
        build_layout(*read_events()),
        console=console,
        refresh_per_second=8,
        screen=True,
    ) as live:
        while True:
            events, ws_ok = read_events()
            live.update(build_layout(events, ws_ok))
            time.sleep(0.125)


if __name__ == "__main__":
    main()
