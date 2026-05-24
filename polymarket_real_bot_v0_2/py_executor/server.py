"""FastAPI server bridging Rust bot ←→ live execution + Grok AI."""

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from dotenv import load_dotenv

# Load .env from parent directory
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from clob_executor import CLOBExecutor
from grok_agent import GrokAgent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)-18s | %(levelname)-8s | %(message)s",
)
logger = logging.getLogger("server")

app = FastAPI(title="Polymarket Execution Server")

# --- Init ---

clob: Optional[CLOBExecutor] = None
grok: Optional[GrokAgent] = None
live_mode: bool = False


def get_clob() -> CLOBExecutor:
    global clob
    if clob is None:
        api_key = os.getenv("POLY_API_KEY", "")
        api_secret = os.getenv("POLY_API_SECRET", "")
        passphrase = os.getenv("POLY_PASSPHRASE", "")
        if not api_key or not api_secret or not passphrase:
            raise HTTPException(503, "CLOB credentials not configured")
        clob = CLOBExecutor(api_key, api_secret, passphrase)
    return clob


def get_grok() -> GrokAgent:
    global grok
    if grok is None:
        api_key = os.getenv("GROK_API_KEY", "")
        if not api_key:
            raise HTTPException(503, "Grok API key not configured")
        grok = GrokAgent(api_key)
    return grok


# --- Models ---

class OrderRequest(BaseModel):
    token_id: str
    price: float
    size: float
    side: str = "BUY"
    order_type: str = "limit"


class MarketAnalysisRequest(BaseModel):
    question: str
    yes_price: float
    volume: float = 0.0
    days_left: Optional[float] = None


class BatchAnalysisRequest(BaseModel):
    markets: list[Dict[str, Any]]


# --- Health ---

@app.get("/health")
def health() -> Dict:
    return {
        "status": "healthy",
        "live_mode": live_mode,
        "clob_configured": bool(os.getenv("POLY_API_KEY")),
        "grok_configured": bool(os.getenv("GROK_API_KEY")),
    }


# --- CLOB Endpoints ---

@app.get("/clob/orders")
def get_orders():
    return get_clob().get_orders()


@app.get("/clob/trades")
def get_trades():
    return get_clob().get_trades()


@app.get("/clob/book/{token_id}")
def get_orderbook(token_id: str):
    return get_clob().get_orderbook(token_id)


@app.post("/clob/order")
def place_order(req: OrderRequest):
    if not live_mode:
        return {"status": "simulated", "message": "Live mode is disabled. POST /live/toggle to enable."}
    if req.order_type == "market":
        result = get_clob().place_market_order(req.token_id, req.size, req.side)
    else:
        result = get_clob().place_limit_order(req.token_id, req.price, req.size, req.side)
    logger.info(f"[LIVE] {req.side} {req.size} @ {req.price} on {req.token_id[:16]}... -> {result.get('orderID', result.get('error', '?'))}")
    return result


@app.delete("/clob/order/{order_id}")
def cancel_order(order_id: str):
    return get_clob().cancel_order(order_id)


@app.delete("/clob/orders")
def cancel_all():
    return get_clob().cancel_all()


# --- Live Mode Gate ---

@app.get("/live/status")
def live_status() -> Dict:
    return {"live_mode": live_mode}


@app.post("/live/toggle")
def toggle_live(gate: str = ""):
    global live_mode
    if gate != "CONFIRM_LIVE":
        live_mode = False
        return {"live_mode": False, "message": "Gate not passed. Use ?gate=CONFIRM_LIVE to enable."}
    live_mode = True
    logger.warning("LIVE MODE ACTIVATED — REAL ORDERS WILL BE PLACED")
    return {"live_mode": True, "message": "LIVE MODE ACTIVE. Real orders will be placed."}


@app.post("/live/disable")
def disable_live():
    global live_mode
    live_mode = False
    return {"live_mode": False}


# --- Grok AI Endpoints ---

@app.post("/grok/analyze")
async def analyze_market(req: MarketAnalysisRequest):
    agent = get_grok()
    result = await agent.analyze_market(
        question=req.question,
        yes_price=req.yes_price,
        volume=req.volume,
        days_left=req.days_left,
    )
    return result


@app.post("/grok/batch-analyze")
async def batch_analyze(req: BatchAnalysisRequest):
    agent = get_grok()
    results = await agent.batch_analyze(req.markets)
    return {"markets": results}


@app.post("/grok/pulse")
async def pulse(topic: str):
    agent = get_grok()
    return await agent.quick_pulse(topic)


# --- Paper Trade Forwarding (from Rust bot) ---

class PaperTradeRequest(BaseModel):
    market_id: str
    condition_id: str
    token_id: str
    outcome: str
    side: str
    price: float
    size_usdc: float
    reason: str


@app.post("/paper/trade")
def record_paper_trade(req: PaperTradeRequest):
    logger.info(f"[PAPER] {req.side} {req.outcome} ${req.size_usdc} @ {req.price} | {req.reason[:80]}")
    return {"status": "recorded", "mode": "paper"}


# --- Shutdown ---

@app.on_event("shutdown")
async def shutdown():
    global clob, grok
    if clob:
        clob.close()
    if grok:
        await grok.close()


if __name__ == "__main__":
    import uvicorn
    logger.info("Starting Polymarket Execution Server...")
    logger.info(f"CLOB configured: {bool(os.getenv('POLY_API_KEY'))}")
    logger.info(f"Grok configured: {bool(os.getenv('GROK_API_KEY'))}")
    uvicorn.run(app, host="127.0.0.1", port=4002, log_level="info")
