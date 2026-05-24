"""FastAPI dashboard — exposes bot state as REST API."""
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

logger = logging.getLogger("poly.dashboard")

app = FastAPI(title="Poly Odds Bot Dashboard", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Shared state — populated by the main bot loop
bot_state: dict = {
    "mode": "paper",
    "running": False,
    "loop_number": 0,
    "bankroll_usd": 1000.0,
    "cash": 0.0,
    "equity": 0.0,
    "total_pnl": 0.0,
    "positions": {},
    "markets": [],
    "signals": [],
    "closed_trades": [],
    "updated_at": None,
}


def update_state(**kwargs):
    """Called by the main loop to push fresh state."""
    bot_state.update(kwargs)
    bot_state["updated_at"] = datetime.now(timezone.utc).isoformat()


@app.get("/")
async def root():
    return {"service": "Poly Odds Bot Dashboard", "version": "1.0"}


@app.get("/status")
async def status():
    """Overall bot status."""
    return {
        "mode": bot_state["mode"],
        "running": bot_state["running"],
        "loop_number": bot_state["loop_number"],
        "updated_at": bot_state["updated_at"],
    }


@app.get("/portfolio")
async def portfolio():
    """Portfolio summary."""
    return {
        "bankroll_usd": bot_state["bankroll_usd"],
        "cash": bot_state["cash"],
        "equity": bot_state["equity"],
        "total_pnl": bot_state["total_pnl"],
        "position_count": len(bot_state["positions"]),
        "closed_trades": len(bot_state["closed_trades"]),
    }


@app.get("/positions")
async def positions():
    """Open positions."""
    return {"positions": list(bot_state["positions"].values())}


@app.get("/markets")
async def markets(limit: int = 20):
    """Active markets with best prices."""
    return {"markets": bot_state["markets"][:limit], "total": len(bot_state["markets"])}


@app.get("/signals")
async def signals(limit: int = 50):
    """Recent signals."""
    return {"signals": bot_state["signals"][-limit:], "total": len(bot_state["signals"])}


@app.get("/trades")
async def trades(limit: int = 50):
    """Closed trades."""
    trades_list = bot_state["closed_trades"]
    return {"trades": trades_list[-limit:], "total": len(trades_list)}


@app.get("/health")
async def health():
    return {"status": "ok", "ts": datetime.now(timezone.utc).isoformat()}


def start_dashboard(host: str = "0.0.0.0", port: int = 8080):
    """Start the dashboard server (blocking, run in a thread)."""
    import uvicorn
    uvicorn.run(app, host=host, port=port, log_level="info")
