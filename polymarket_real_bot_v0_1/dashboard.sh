#!/bin/bash
# ══════════════════════════════════════════════════════════════════════════════
# CHAIN GAMBLER v0.3 — LIVE TERMINAL DASHBOARD
# Reads real CLOB balance from live_dashboard.json + fills from live_fills.jsonl
# Run in a SEPARATE terminal while the bot is running.
# ══════════════════════════════════════════════════════════════════════════════

cd "$(dirname "$0")" || exit 1
REFRESH=2

while true; do
  clear

  LIVE_FILE="live_dashboard.json"
  FILLS_FILE="live_fills.jsonl"
  ORDERS_FILE="live_orders.jsonl"

  if [ ! -f "$LIVE_FILE" ]; then
    echo ""
    echo -e "\033[38;5;196m\033[1m  ⚠  Waiting for bot — $LIVE_FILE not found\033[0m"
    echo -e "\033[38;5;245m  Start the bot: cargo run --release -- run --live --config config.toml\033[0m"
    sleep 3
    continue
  fi

  python3 << 'PYEOF'
import json, sys, os, re
from datetime import datetime, timezone

# ─── Theme ───
R  = '\033[0m'
B  = '\033[1m'
D  = '\033[2m'

# Palette
ACCENT = '\033[38;5;141m'
CYAN   = '\033[38;5;81m'
GREEN  = '\033[38;5;114m'
RED    = '\033[38;5;204m'
YELLOW = '\033[38;5;222m'
ORANGE = '\033[38;5;215m'
BLUE   = '\033[38;5;111m'
GRAY   = '\033[38;5;245m'
DGRAY  = '\033[38;5;238m'
WHITE  = '\033[38;5;255m'
PINK   = '\033[38;5;213m'

# Traffic lights
LIGHT_GREEN  = '\033[38;5;46m'
LIGHT_RED    = '\033[38;5;196m'
LIGHT_YELLOW = '\033[38;5;226m'
LIGHT_OFF    = '\033[38;5;240m'

W = 66

def vlen(s):
    return len(re.sub(r'\033\[[^m]*m', '', s))

def box_top(color=DGRAY):
    return f"  {color}╭{'─' * W}╮{R}"

def box_mid(color=DGRAY):
    return f"  {color}├{'─' * W}┤{R}"

def box_bot(color=DGRAY):
    return f"  {color}╰{'─' * W}╯{R}"

def box_line(content, color=DGRAY):
    pad = W - vlen(content)
    if pad < 0: pad = 0
    return f"  {color}│{R} {content}{' ' * pad}{color}│{R}"

def box_header(icon, title, color=DGRAY, title_color=WHITE):
    content = f"{title_color}{B}{icon} {title}{R}"
    visible = f"{icon} {title}"
    pad = W - len(visible) - 1
    return f"  {color}│{R} {content}{' ' * pad}{color}│{R}"

def fmt_usd(v):
    v = float(v)
    return f"${v:,.2f}" if v >= 0 else f"-${abs(v):,.2f}"

def light(on, color=LIGHT_GREEN):
    """Traffic light dot: bright when on, dim when off"""
    return f"{color}●{R}" if on else f"{LIGHT_OFF}○{R}"

def traffic(status):
    """3-state traffic light: 'green', 'yellow', 'red'"""
    if status == 'green':
        return f"{LIGHT_GREEN}●{R} {LIGHT_OFF}○{R} {LIGHT_OFF}○{R}"
    elif status == 'yellow':
        return f"{LIGHT_OFF}○{R} {LIGHT_YELLOW}●{R} {LIGHT_OFF}○{R}"
    else:
        return f"{LIGHT_OFF}○{R} {LIGHT_OFF}○{R} {LIGHT_RED}●{R}"

def bar(pct, width=16, fill_color=GREEN, empty_color=DGRAY):
    filled = int(pct / 100 * width)
    empty = width - filled
    return f"{fill_color}{'█' * filled}{empty_color}{'░' * empty}{R}"

# ─── Read Data ───
try:
    with open("live_dashboard.json") as f:
        live = json.loads(f.read().strip())
except Exception as e:
    print(f"{RED}Cannot read live_dashboard.json: {e}{R}")
    sys.exit(0)

balance     = float(live.get("clob_balance", "0"))
equity      = float(live.get("equity", "0"))
pos_value   = float(live.get("position_value", "0"))
pos_count   = int(live.get("position_count", 0))
iteration   = int(live.get("iteration", 0))
loop_buys   = int(live.get("loop_buys", 0))
loop_sells  = int(live.get("loop_sells", 0))
loop_rej    = int(live.get("loop_rejected", 0))
tot_placed  = int(live.get("total_orders_placed", 0))
tot_failed  = int(live.get("total_orders_failed", 0))
selected    = int(live.get("selected_markets", 0))
total_mkts  = int(live.get("total_markets", 0))
positions   = live.get("positions", [])
ts          = live.get("timestamp", "")

try:
    dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
    time_str = dt.strftime("%H:%M:%S UTC")
    ago = (datetime.now(timezone.utc) - dt).total_seconds()
    ago_str = f"{int(ago)}s ago"
except:
    time_str = "??:??:??"
    ago = 999
    ago_str = "?"

# Fills
fills = []
if os.path.exists("live_fills.jsonl"):
    with open("live_fills.jsonl") as f:
        for line in f:
            line = line.strip()
            if line:
                try: fills.append(json.loads(line))
                except: pass

# Orders
orders = []
if os.path.exists("live_orders.jsonl"):
    with open("live_orders.jsonl") as f:
        for line in f:
            line = line.strip()
            if line:
                try: orders.append(json.loads(line))
                except: pass

total_fills = len(fills)
fill_volume = sum(float(f.get("size_matched", "0")) for f in fills)
total_resting = len(orders)

# ─── Compute Health Status ───
success_rate = (tot_placed / (tot_placed + tot_failed) * 100) if (tot_placed + tot_failed) > 0 else 0
fill_rate = (total_fills / tot_placed * 100) if tot_placed > 0 else 0

# Heartbeat: green < 10s, yellow < 30s, red > 30s
hb_status = 'green' if ago < 10 else ('yellow' if ago < 30 else 'red')

# Connection: green if markets found
conn_status = 'green' if selected > 0 else ('yellow' if total_mkts > 0 else 'red')

# Orders: green if placing, yellow if mostly failing, red if nothing
if tot_placed == 0 and tot_failed == 0:
    order_status = 'yellow'
elif success_rate > 5:
    order_status = 'green'
elif tot_placed > 0:
    order_status = 'yellow'
else:
    order_status = 'red'

# Fills: green if getting fills, yellow if placing but no fills, red if nothing
if total_fills > 0:
    fill_status = 'green'
elif tot_placed > 0:
    fill_status = 'yellow'
else:
    fill_status = 'red'

# Balance: green > $10, yellow > $3, red < $3
if balance > 10:
    bal_status = 'green'
elif balance > 3:
    bal_status = 'yellow'
else:
    bal_status = 'red'

# Positions: info only — green if has positions, off if not
pos_status = 'green' if pos_count > 0 else 'yellow'

# ═══════════════════════════════════════════════════════════════════
#                         R E N D E R
# ═══════════════════════════════════════════════════════════════════

print()

# ─── Header ───
print(f"  {ACCENT}{B}  ⚡ CHAIN GAMBLER v0.3{R}  {DGRAY}│{R}  {CYAN}{B}LIVE CLOB DASHBOARD{R}")
hb_dot = f"{LIGHT_GREEN}●{R}" if hb_status == 'green' else (f"{LIGHT_YELLOW}●{R}" if hb_status == 'yellow' else f"{LIGHT_RED}●{R}")
print(f"  {GRAY}  Loop {WHITE}{B}#{iteration}{R}  {DGRAY}│{R}  {hb_dot} {GRAY}{time_str} ({ago_str}){R}")
print()

# ═══════════════════════════════════════════════════════════════════
# SYSTEM STATUS — TRAFFIC LIGHTS
# ═══════════════════════════════════════════════════════════════════
print(box_top(ACCENT))
print(box_header("🚦", "SYSTEM STATUS", ACCENT, ACCENT))
print(box_mid(ACCENT))
print(box_line(f"  {traffic(hb_status)}  {WHITE}Heartbeat{R}     {traffic(conn_status)}  {WHITE}Markets{R}      {traffic(bal_status)}  {WHITE}Balance{R}"))
print(box_line(f"  {traffic(order_status)}  {WHITE}Orders{R}        {traffic(fill_status)}  {WHITE}Fills{R}        {traffic(pos_status)}  {WHITE}Positions{R}"))
print(box_bot(ACCENT))
print()

# ═══════════════════════════════════════════════════════════════════
# BALANCE CARD
# ═══════════════════════════════════════════════════════════════════
bc = CYAN
print(box_top(bc))
print(box_header("💰", "CLOB BALANCE", bc, CYAN))
print(box_mid(bc))
bal_clr = GREEN if balance > 10 else (YELLOW if balance > 3 else RED)
print(box_line(f"  {GRAY}USDC Cash{R}        {bal_clr}{B}{fmt_usd(balance):>14}{R}"))
print(box_line(f"  {GRAY}Positions{R}        {WHITE}{B}{fmt_usd(pos_value):>14}{R}  {GRAY}({pos_count} open){R}"))
print(box_mid(bc))
eq_clr = GREEN if equity > 30 else (YELLOW if equity > 10 else RED)
print(box_line(f"  {CYAN}{B}EQUITY{R}           {eq_clr}{B}{fmt_usd(equity):>14}{R}"))
print(box_bot(bc))
print()

# ═══════════════════════════════════════════════════════════════════
# ORDER FLOW CARD
# ═══════════════════════════════════════════════════════════════════
oc = ORANGE
print(box_top(oc))
print(box_header("📊", "ORDER FLOW", oc, ORANGE))
print(box_mid(oc))
print(box_line(f"  {GRAY}Placed{R}    {GREEN}{B}{tot_placed:>6,}{R}       {GRAY}Failed{R}    {RED}{B}{tot_failed:>6,}{R}"))
print(box_line(f"  {GRAY}Fills{R}     {GREEN}{B}{total_fills:>6,}{R}       {GRAY}Resting{R}   {BLUE}{B}{total_resting:>6,}{R}"))
print(box_line(f"  {GRAY}Volume{R}    {WHITE}{B}{fmt_usd(fill_volume):>14}{R}"))
print(box_mid(oc))

sr_clr = GREEN if success_rate > 10 else (YELLOW if success_rate > 2 else RED)
fr_clr = GREEN if fill_rate > 30 else (YELLOW if fill_rate > 5 else RED)
sr_bar = bar(success_rate, 14, sr_clr, DGRAY)
fr_bar = bar(fill_rate, 14, fr_clr, DGRAY)
print(box_line(f"  {GRAY}Success{R}  {sr_bar}  {sr_clr}{B}{success_rate:>4.0f}%{R}"))
print(box_line(f"  {GRAY}Fill{R}     {fr_bar}  {fr_clr}{B}{fill_rate:>4.0f}%{R}"))
print(box_mid(oc))
print(box_line(f"  {GRAY}Markets{R}   {WHITE}{B}{selected:>3}{R} {GRAY}active{R}  {DGRAY}/{R}  {WHITE}{total_mkts}{R} {GRAY}discovered{R}"))
buy_s  = f"{GREEN}▲{loop_buys} buy{R}"
sell_s = f"{RED}▼{loop_sells} sell{R}"
rej_s  = f"{YELLOW}✗{loop_rej} skip{R}"
print(box_line(f"  {GRAY}This Loop{R} {buy_s}   {sell_s}   {rej_s}"))
print(box_bot(oc))
print()

# ═══════════════════════════════════════════════════════════════════
# POSITIONS
# ═══════════════════════════════════════════════════════════════════
if positions:
    gc = GREEN
    print(box_top(gc))
    print(box_header("📈", f"POSITIONS ({len(positions)})", gc, GREEN))
    print(box_mid(gc))
    print(box_line(f"  {DGRAY}{'OUT':<5} {'SHARES':>8}   {'AVG':>7}   {'COST':>8}   {'ID':>12}{R}"))
    print(box_mid(gc))
    for pos in positions[:8]:
        outcome = pos.get("outcome", "?")
        shares = float(pos.get("shares", "0"))
        cost = float(pos.get("cost_usdc", "0"))
        avg = cost / shares if shares > 0 else 0
        mid = pos.get("market_id", "???")[:10]
        clr = GREEN if outcome == "YES" else RED
        icon = "🟢" if outcome == "YES" else "🔴"
        print(box_line(f"  {clr}{icon}{outcome:<4}{R} {WHITE}{shares:>8.1f}{R}   {GRAY}@${avg:.3f}{R}   {WHITE}{fmt_usd(cost):>8}{R}   {DGRAY}#{mid}{R}"))
    if len(positions) > 8:
        print(box_line(f"  {GRAY}… +{len(positions)-8} more{R}"))
    print(box_bot(gc))
    print()

# ═══════════════════════════════════════════════════════════════════
# LIVE FILLS
# ═══════════════════════════════════════════════════════════════════
recent_fills = fills[-10:] if fills else []
yc = YELLOW
print(box_top(yc))
if total_fills > 0:
    print(box_header("✓", f"LIVE FILLS — {total_fills} total  ·  {fmt_usd(fill_volume)} volume", yc, YELLOW))
else:
    print(box_header("◌", "LIVE FILLS — awaiting first fill", yc, YELLOW))
print(box_mid(yc))

if recent_fills:
    print(box_line(f"  {DGRAY}{'TIME':<10} {'SIDE':<6} {'OUT':<4} {'PRICE':>7} {'FILLED':>8}  {'ORDER':>18}{R}"))
    print(box_mid(yc))
    for ev in reversed(recent_fills):
        ts_e = ev.get("timestamp", "")
        try:
            dt_e = datetime.fromisoformat(ts_e.replace('Z', '+00:00'))
            t_str = dt_e.strftime("%H:%M:%S")
        except:
            t_str = "??:??:??"
        side = ev.get("side", "?")
        outcome = ev.get("outcome", "?")
        price = float(ev.get("price", "0"))
        matched = float(ev.get("size_matched", "0"))
        oid = ev.get("order_id", "???")[:14]
        side_clr = GREEN if side == "BUY" else RED
        side_icon = "▲" if side == "BUY" else "▼"
        out_clr = GREEN if outcome == "YES" else RED
        fill_dot = f"{LIGHT_GREEN}●{R}"
        print(box_line(f"  {fill_dot} {GRAY}{t_str}{R} {side_clr}{B}{side_icon}{side:<4}{R} {out_clr}{outcome:<4}{R} {WHITE}${price:.2f}{R}   {WHITE}${matched:.2f}{R}   {DGRAY}{oid}…{R}"))
else:
    print(box_line(f"  {GRAY}Maker orders on the book — waiting for counterparty …{R}"))
    print(box_line(f"  {DGRAY}Orders placed: {tot_placed}  |  Most rest unfilled in 5m markets{R}"))

print(box_bot(yc))
print()

# ═══════════════════════════════════════════════════════════════════
# FOOTER
# ═══════════════════════════════════════════════════════════════════
print(f"  {DGRAY}{'─' * W}{R}")
print(f"  {GRAY}  Ctrl+C to exit  {DGRAY}│{R}  {GRAY}Source: {CYAN}{B}REAL CLOB{R} {GRAY}balance{R}")
print()

PYEOF

  sleep $REFRESH
done
