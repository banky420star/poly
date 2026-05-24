"""Polymarket CLOB REST client — fetches order book snapshots."""
import asyncio
import logging
from typing import Optional

import httpx

from app.data.orderbook_state import OrderBookState

logger = logging.getLogger("poly.clob_rest")

CLOB_API_URL = "https://clob.polymarket.com"


class PolymarketClient:
    """Thin async REST client for CLOB endpoints."""

    def __init__(self, base_url: str = CLOB_API_URL, ssl_verify: bool = False):
        self.base_url = base_url
        self.ssl_verify = ssl_verify
        self.client: httpx.AsyncClient | None = None

    async def start(self):
        self.client = httpx.AsyncClient(timeout=15.0, verify=self.ssl_verify)

    async def close(self):
        if self.client:
            await self.client.aclose()
            self.client = None

    async def fetch_order_book(self, token_id: str) -> Optional[OrderBookState]:
        """Fetch order book snapshot for a single token.

        Public endpoint: GET /book?token_id=...
        Returns best bid/ask with depth.
        """
        if not self.client:
            return None

        url = f"{self.base_url}/book"
        params = {"token_id": token_id}

        try:
            resp = await self.client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning("Failed to fetch order book for %s: %s", token_id, e)
            return None

        try:
            bids = data.get("bids", [])
            asks = data.get("asks", [])

            best_bid = float(bids[0]["price"]) if bids else 0.0
            best_ask = float(asks[0]["price"]) if asks else 0.0

            bid_depth = sum(float(b["size"]) * float(b["price"]) for b in bids[:5]) if bids else 0.0
            ask_depth = sum(float(a["size"]) * float(a["price"]) for a in asks[:5]) if asks else 0.0

            return OrderBookState(
                token_id=token_id,
                best_bid=round(best_bid, 4),
                best_ask=round(best_ask, 4),
                bid_depth=round(bid_depth, 2),
                ask_depth=round(ask_depth, 2),
            )
        except (KeyError, ValueError, IndexError) as e:
            logger.warning("Failed to parse order book for %s: %s", token_id, e)
            return None

    async def fetch_all_books(self, token_ids: list[str]) -> dict[str, OrderBookState]:
        """Fetch order books for multiple tokens concurrently."""
        tasks = [self.fetch_order_book(tid) for tid in token_ids]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        books = {}
        for token_id, result in zip(token_ids, results):
            if isinstance(result, OrderBookState) and result is not None:
                books[token_id] = result
        return books

    async def get_midpoint(self, token_id: str) -> Optional[float]:
        """Get the midpoint price for a token (best_bid + best_ask) / 2."""
        book = await self.fetch_order_book(token_id)
        if book is None or book.best_bid <= 0 or book.best_ask <= 0:
            return None
        return round((book.best_bid + book.best_ask) / 2.0, 4)
