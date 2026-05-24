"""Polymarket WebSocket client — streams live order book updates."""
import asyncio
import json
import logging
from typing import Optional

import websockets

from app.data.orderbook_state import OrderBookState

logger = logging.getLogger("poly.poly_ws")

POLY_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


class PolymarketWsClient:
    """Connects to Polymarket WebSocket and maintains live order book snapshots."""

    def __init__(self):
        self.books: dict[str, OrderBookState] = {}
        self.token_ids: list[str] = []
        self.running = False
        self.connected = False
        self._task: asyncio.Task | None = None

    def subscribe(self, token_ids: list[str]):
        """Set which tokens to subscribe to."""
        self.token_ids = list(set(token_ids))

    async def start(self):
        """Start the WebSocket connection in the background."""
        if not self.token_ids:
            logger.warning("Polymarket WS: no tokens to subscribe to")
            return

        self.running = True
        self._task = asyncio.create_task(self._run())
        logger.info("Polymarket WS started with %d tokens", len(self.token_ids))

    async def _run(self):
        while self.running:
            try:
                async with websockets.connect(POLY_WS_URL) as ws:
                    self.connected = True
                    logger.info("Polymarket WS connected")

                    # Subscribe to each token's market channel
                    for token_id in self.token_ids:
                        sub_msg = {
                            "type": "market",
                            "assets_ids": [token_id],
                        }
                        await ws.send(json.dumps(sub_msg))

                    async for message in ws:
                        if not self.running:
                            break
                        self._handle_message(message)

                    self.connected = False
            except Exception as e:
                self.connected = False
                logger.warning("Polymarket WS disconnected: %s. Reconnecting in 5s...", e)
                if self.running:
                    await asyncio.sleep(5)

    def _handle_message(self, raw: str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return

        event_type = data.get("event_type", "")

        if event_type == "book":
            self._handle_book(data)
        elif event_type == "price_change":
            self._handle_price_change(data)
        elif event_type == "last_trade_price":
            self._handle_last_trade(data)

    def _handle_book(self, data: dict):
        """Parse a book event into OrderBookState."""
        asset_id = data.get("asset_id", "")
        if not asset_id:
            return

        bids = data.get("bids", [])
        asks = data.get("asks", [])

        best_bid = float(bids[0]["price"]) if bids else 0.0
        best_ask = float(asks[0]["price"]) if asks else 0.0

        bid_depth = sum(float(b["size"]) * float(b["price"]) for b in bids[:5]) if bids else 0.0
        ask_depth = sum(float(a["size"]) * float(a["price"]) for a in asks[:5]) if asks else 0.0

        if asset_id in self.books:
            book = self.books[asset_id]
            book.best_bid = best_bid
            book.best_ask = best_ask
            book.bid_depth = bid_depth
            book.ask_depth = ask_depth
        else:
            self.books[asset_id] = OrderBookState(
                token_id=asset_id,
                best_bid=round(best_bid, 4),
                best_ask=round(best_ask, 4),
                bid_depth=round(bid_depth, 2),
                ask_depth=round(ask_depth, 2),
            )

    def _handle_price_change(self, data: dict):
        """Update best bid/ask from a price change event."""
        asset_id = data.get("asset_id", "")
        if not asset_id:
            return

        price_str = data.get("price", "0")
        side = data.get("side", "")

        try:
            price = float(price_str)
        except (ValueError, TypeError):
            return

        if asset_id not in self.books:
            self.books[asset_id] = OrderBookState(
                token_id=asset_id,
                best_bid=price if side == "BUY" else 0.0,
                best_ask=price if side == "SELL" else 0.0,
            )
        else:
            book = self.books[asset_id]
            if side == "BUY":
                book.best_bid = price
            elif side == "SELL":
                book.best_ask = price

    def _handle_last_trade(self, data: dict):
        """Update last trade info."""
        asset_id = data.get("asset_id", "")
        if not asset_id:
            return

        price_str = data.get("price", "0")
        side = data.get("side", "")

        try:
            price = float(price_str)
        except (ValueError, TypeError):
            return

        if asset_id in self.books:
            self.books[asset_id].last_trade_price = price
            self.books[asset_id].last_trade_side = side

    async def stop(self):
        """Stop the WebSocket feed."""
        self.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self.connected = False
        logger.info("Polymarket WS stopped")

    def get(self, token_id: str) -> Optional[OrderBookState]:
        """Get latest order book for a token."""
        return self.books.get(token_id)

    def get_pair(self, up_token_id: str, down_token_id: str) -> tuple[Optional[OrderBookState], Optional[OrderBookState]]:
        """Get both sides of a binary market."""
        return (self.books.get(up_token_id), self.books.get(down_token_id))
