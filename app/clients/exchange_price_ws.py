"""Binance WebSocket price feed — streams BTC/ETH/SOL spot prices into a shared PriceState dict."""
import asyncio
import json
import logging
from datetime import datetime, timezone

import websockets

from app.data.price_state import PriceState

logger = logging.getLogger("poly.binance_ws")

BINANCE_WS_URL = "wss://stream.binance.com:9443/ws"


class BinancePriceFeed:
    """Connects to Binance WebSocket and maintains live spot prices."""

    def __init__(self, symbols: list[str] | None = None):
        self.symbols = symbols or ["BTC", "ETH", "SOL"]
        self.prices: dict[str, PriceState] = {}
        self.running = False
        self._task: asyncio.Task | None = None

    def _stream_name(self) -> str:
        """Build combined stream name like btcusdt@ticker/ethusdt@ticker"""
        streams = [f"{s.lower()}usdt@ticker" for s in self.symbols]
        return "/".join(streams)

    async def start(self):
        """Start the WebSocket connection in the background."""
        self.running = True
        self._task = asyncio.create_task(self._run())
        logger.info("Binance feed started for %s", self.symbols)

    async def _run(self):
        url = f"{BINANCE_WS_URL}/{self._stream_name()}"
        while self.running:
            try:
                async with websockets.connect(url) as ws:
                    logger.info("Binance WS connected")
                    async for message in ws:
                        if not self.running:
                            break
                        self._handle_message(message)
            except Exception as e:
                logger.warning("Binance WS disconnected: %s. Reconnecting in 5s...", e)
                if self.running:
                    await asyncio.sleep(5)

    def _handle_message(self, raw: str):
        try:
            data = json.loads(raw)
            if "stream" not in data:
                return

            stream = data["stream"]  # e.g. "btcusdt@ticker"
            ticker = data.get("data", {})

            symbol = stream.split("@")[0].upper().rstrip("USDT")  # "BTCUSDT" -> "BTC"
            price_str = ticker.get("c")  # Current price
            close_time = ticker.get("C", 0)  # Close time in ms

            if price_str is None:
                return

            price = float(price_str)
            self.prices[symbol] = PriceState(
                symbol=symbol,
                price=price,
                timestamp_ms=int(close_time) if close_time else int(datetime.now(timezone.utc).timestamp() * 1000),
                source="binance",
            )
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.debug("Binance parse error: %s", e)

    async def stop(self):
        """Stop the WebSocket feed."""
        self.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Binance feed stopped")

    def get(self, symbol: str) -> PriceState | None:
        """Get latest price for a symbol. Returns None if no data yet."""
        return self.prices.get(symbol.upper())
