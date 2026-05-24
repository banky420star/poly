"""Market discovery — finds active Polymarket Up/Down binary markets via Gamma API."""
import asyncio
import json
import logging
from datetime import datetime
from typing import Optional

import httpx

from app.data.market_state import MarketState

logger = logging.getLogger("poly.discovery")

GAMMA_API_URL = "https://gamma-api.polymarket.com"


class MarketDiscovery:
    """Discovers active Up/Down markets from the Polymarket Gamma API."""

    def __init__(self, symbols: list[str] | None = None, timeframes: list[str] | None = None):
        self.symbols = symbols or ["BTC", "ETH", "SOL"]
        self.timeframes = timeframes or ["5m", "15m"]

    def _slug_pattern(self, symbol: str, timeframe: str) -> str:
        """Build expected slug pattern for a symbol+timeframe combination."""
        sym = symbol.lower()
        if timeframe == "5m":
            return f"{sym}-updown-5m"
        if timeframe == "15m":
            return f"{sym}-updown-15m"
        return f"{sym}-updown"

    async def discover(self, client: httpx.AsyncClient | None = None) -> list[MarketState]:
        """Discover all matching markets. Returns list of MarketState objects."""
        close_client = False
        if client is None:
            client = httpx.AsyncClient(timeout=30.0)
            close_client = True

        try:
            tasks = []
            for symbol in self.symbols:
                tasks.append(self._fetch_symbol(client, symbol))

            results = await asyncio.gather(*tasks, return_exceptions=True)

            markets: list[MarketState] = []
            seen_ids: set[str] = set()

            for result in results:
                if isinstance(result, list):
                    for market in result:
                        if market.market_id not in seen_ids:
                            seen_ids.add(market.market_id)
                            markets.append(market)
                elif isinstance(result, Exception):
                    logger.warning("Market discovery failed for a symbol: %s", result)

            logger.info("Discovered %d markets across %d symbols", len(markets), len(self.symbols))
            return markets

        finally:
            if close_client:
                await client.aclose()

    async def _fetch_symbol(self, client: httpx.AsyncClient, symbol: str) -> list[MarketState]:
        """Fetch all markets for a single symbol."""
        url = f"{GAMMA_API_URL}/markets"
        params = {
            "active": "true",
            "closed": "false",
            "limit": 50,
            "slug_contains": symbol.lower(),
        }

        try:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning("Failed to fetch %s markets: %s", symbol, e)
            return []

        markets = []
        for raw in data:
            slug = raw.get("slug", "").lower()
            question = raw.get("question", "")

            # Determine timeframe from slug
            detected_timeframe = None
            for tf in self.timeframes:
                if self._slug_pattern(symbol, tf) in slug:
                    detected_timeframe = tf
                    break

            if not detected_timeframe:
                continue

            # Extract CLOB token IDs
            clob_tokens = raw.get("clobTokenIds", "[]")
            if isinstance(clob_tokens, str):
                try:
                    clob_tokens = json.loads(clob_tokens)
                except json.JSONDecodeError:
                    continue

            if not clob_tokens or len(clob_tokens) < 2:
                continue

            up_token_id = clob_tokens[0] if isinstance(clob_tokens[0], str) else clob_tokens[0].get("id", "")
            down_token_id = clob_tokens[1] if isinstance(clob_tokens[1], str) else clob_tokens[1].get("id", "")

            if not up_token_id or not down_token_id:
                continue

            # Extract price_to_beat from outcomePrices
            price_to_beat = 0.0
            outcome_prices = raw.get("outcomePrices", "[]")
            if isinstance(outcome_prices, str):
                try:
                    outcome_prices = json.loads(outcome_prices)
                except json.JSONDecodeError:
                    outcome_prices = []
            if isinstance(outcome_prices, list) and len(outcome_prices) > 0:
                try:
                    price_to_beat = float(outcome_prices[0])
                except (ValueError, TypeError):
                    pass

            # Parse timestamps
            start_ts = raw.get("startDateIso")
            end_ts = raw.get("endDateIso")

            try:
                start_time = datetime.fromisoformat(start_ts.replace("Z", "+00:00")) if start_ts else datetime.min
                end_time = datetime.fromisoformat(end_ts.replace("Z", "+00:00")) if end_ts else datetime.max
            except (ValueError, AttributeError):
                continue

            market = MarketState(
                market_id=str(raw.get("id", "")),
                question=question,
                slug=slug,
                symbol=symbol.upper(),
                timeframe=detected_timeframe,
                up_token_id=up_token_id,
                down_token_id=down_token_id,
                price_to_beat=price_to_beat,
                start_time=start_time,
                end_time=end_time,
                active=raw.get("active", True),
                tick_size=float(raw.get("orderPriceMinTickSize", 0.01)),
            )
            markets.append(market)

        return markets
