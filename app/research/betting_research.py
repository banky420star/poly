"""Betting research agent — analyzes live markets and places paper bets.

Runs a complete research cycle:
1. Discover current-window Up/Down markets via Gamma API
2. Compute momentum from Binance 1m klines
3. Fetch real CLOB order books and last trade prices
4. Calculate edge (fair_prob - market_price) for each market
5. Place a paper bet on the best opportunity if edge > threshold
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone

import httpx

from app.config import load_config
from app.discovery.market_discovery import MarketDiscovery, TIMEFRAME_SECONDS
from app.data.price_state import PriceState
from app.execution.position_manager import PositionManager
from app.execution.paper_executor import PaperExecutor
from app.storage.recorder import Recorder
from app.agents.entry_timing_agent import EntryDecision

logger = logging.getLogger("poly.research")

MIN_EDGE_THRESHOLD = 0.02
MIN_SECONDS_REMAINING = 20
MAX_ORDER_BOOK_SPREAD = 0.10
KELLY_FRACTION = 0.25
SYMBOLS = ["BTC", "ETH", "SOL"]
TIMEFRAMES = ["5m", "15m"]


async def fetch_klines(client: httpx.AsyncClient, symbol: str) -> list[dict] | None:
    """Fetch recent 1m klines from Binance for momentum calculation."""
    try:
        resp = await client.get(
            f"https://api.binance.com/api/v3/klines",
            params={"symbol": f"{symbol}USDT", "interval": "1m", "limit": 10},
        )
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        logger.debug("Binance klines failed for %s: %s", symbol, e)
    return None


def compute_momentum(klines: list[dict]) -> float:
    """Compute short-term momentum score from klines. Returns -1.0 to 1.0."""
    if not klines or len(klines) < 3:
        return 0.0
    closes = [float(c[4]) for c in klines[-5:]]
    changes = [
        (closes[i] - closes[i - 1]) / closes[i - 1]
        for i in range(1, len(closes))
    ]
    avg_pct = sum(changes) / len(changes) if changes else 0.0
    return max(-1.0, min(1.0, avg_pct * 1000))


async def fetch_book(client: httpx.AsyncClient, token_id: str) -> dict | None:
    """Fetch raw CLOB order book for a token."""
    try:
        resp = await client.get(
            f"https://clob.polymarket.com/book",
            params={"token_id": token_id},
        )
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        logger.debug("CLOB book failed for %s: %s", token_id, e)
    return None


async def research_cycle() -> dict | None:
    """Run one full research-and-bet cycle. Returns bet result or None."""
    cfg = load_config()
    client = httpx.AsyncClient(timeout=30.0, verify=False)
    recorder = Recorder()

    try:
        # 1. Discover current-window markets
        discovery = MarketDiscovery(symbols=SYMBOLS, timeframes=TIMEFRAMES)
        all_markets = await discovery.discover(client)

        now = datetime.now(timezone.utc)
        tradeable = []
        for m in all_markets:
            sec_left = max(0, int((m.end_time - now).total_seconds()))
            if sec_left > MIN_SECONDS_REMAINING:
                tradeable.append((m, sec_left))

        logger.info("Research: %d discovered, %d tradeable", len(all_markets), len(tradeable))

        if not tradeable:
            await client.aclose()
            return None

        # 2. Compute momentum from Binance
        momentum = {}
        spot_map = {}
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

        for sym in SYMBOLS:
            klines = await fetch_klines(client, sym)
            if klines:
                momentum[sym] = compute_momentum(klines)
                price = float(klines[-1][4])
                spot_map[sym] = PriceState(
                    symbol=sym, price=price, timestamp_ms=now_ms, source="binance_rest"
                )

        # 3. Analyze each market
        candidates = []
        for market, sec_left in tradeable:
            up_raw = await fetch_book(client, market.up_token_id)
            down_raw = await fetch_book(client, market.down_token_id)
            if up_raw is None or down_raw is None:
                continue

            mom = momentum.get(market.symbol, 0.0)
            spot = spot_map.get(market.symbol)
            if spot is None:
                continue

            # Market prices from last trades
            up_last = float(up_raw.get("last_trade_price", 0)) or 0.5
            down_last = float(down_raw.get("last_trade_price", 0)) or 0.5

            # Book spreads
            up_bid = float(up_raw["bids"][0]["price"]) if up_raw.get("bids") else 0.0
            up_ask = float(up_raw["asks"][0]["price"]) if up_raw.get("asks") else 1.0
            down_bid = float(down_raw["bids"][0]["price"]) if down_raw.get("bids") else 0.0
            down_ask = float(down_raw["asks"][0]["price"]) if down_raw.get("asks") else 1.0

            up_spread = up_ask - up_bid
            down_spread = down_ask - down_bid

            # Realistic fill: use last_trade when book spread is wide (>10%)
            up_fill = up_last if up_spread > MAX_ORDER_BOOK_SPREAD else up_ask
            down_fill = down_last if down_spread > MAX_ORDER_BOOK_SPREAD else down_ask
            up_fill = max(0.02, min(0.98, up_fill))
            down_fill = max(0.02, min(0.98, down_fill))

            # Fair probability from momentum signal
            fair_up = 0.50 + (mom * 0.35)
            fair_up = max(0.10, min(0.90, fair_up))
            fair_down = 1.0 - fair_up

            up_edge = fair_up - up_fill
            down_edge = fair_down - down_fill

            best_edge = max(up_edge, down_edge)
            best_side = "UP" if up_edge >= down_edge else "DOWN"
            best_fill = up_fill if best_side == "UP" else down_fill
            best_fair = fair_up if best_side == "UP" else fair_down

            # Record signal
            recorder.write("research_signal", {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "symbol": market.symbol,
                "timeframe": market.timeframe,
                "sec_left": sec_left,
                "spot": spot.price,
                "momentum": mom,
                "fair_up": fair_up,
                "up_last": up_last,
                "down_last": down_last,
                "up_edge": up_edge,
                "down_edge": down_edge,
                "best_side": best_side,
                "best_edge": best_edge,
            })

            if best_edge > MIN_EDGE_THRESHOLD:
                candidates.append({
                    "market": market,
                    "side": best_side,
                    "edge": best_edge,
                    "fill_price": best_fill,
                    "fair_prob": best_fair,
                    "spot": spot.price,
                    "mom": mom,
                    "sec_left": sec_left,
                })

        # 4. Place best paper bet
        if not candidates:
            logger.info("Research: no edges above %.1f%%", MIN_EDGE_THRESHOLD * 100)
            await client.aclose()
            return None

        candidates.sort(key=lambda x: x["edge"], reverse=True)
        best = candidates[0]

        positions = PositionManager()
        paper = PaperExecutor(
            position_manager=positions,
            bankroll_usd=float(cfg.raw["risk"]["bankroll_usd"]),
            max_position_pct=float(cfg.raw["risk"]["max_position_pct"]),
        )

        decision = EntryDecision(
            action=f"BUY_{best['side']}_NOW",
            side=best["side"],
            limit_price=best["fill_price"],
            reason=f"mom={best['mom']:+.3f} edge={best['edge']:+.3f} "
                   f"sec_left={best['sec_left']}",
        )
        result = paper.execute_entry(best["market"].market_id, decision)

        # Build summary
        bet_summary = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": best["market"].symbol,
            "timeframe": best["market"].timeframe,
            "slug": best["market"].slug,
            "side": best["side"],
            "fill_price": best["fill_price"],
            "edge": best["edge"],
            "fair_prob": best["fair_prob"],
            "momentum": best["mom"],
            "spot": best["spot"],
            "sec_left": best["sec_left"],
            "size_usd": result.get("size_usd", 0),
            "status": result.get("status", "UNKNOWN"),
        }
        recorder.write("paper_bet", bet_summary)

        logger.info("BET: %s %s %s edge=%+.3f size=$%.2f",
                     best["market"].symbol, best["market"].timeframe,
                     best["side"], best["edge"], result.get("size_usd", 0))

        await client.aclose()
        return bet_summary

    except Exception as e:
        logger.exception("Research cycle failed: %s", e)
        await client.aclose()
        return None


def run_research() -> dict | None:
    """Synchronous wrapper for the research cycle."""
    return asyncio.run(research_cycle())
