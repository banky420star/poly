"""Market discovery — finds active Polymarket Up/Down binary markets via Gamma API.

Up/Down crypto markets use ephemeral slugs like btc-updown-5m-{unix_timestamp}.
They are hidden from standard Gamma queries ("Hide From New" tag) and must be
discovered by computing current window timestamps and probing exact slugs.

See the Rust bot's gamma.rs for the reference implementation.
"""
import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional

import httpx

from app.data.market_state import MarketState

logger = logging.getLogger("poly.discovery")

GAMMA_API_URL = "https://gamma-api.polymarket.com"

# Asset slugs as Polymarket uses them
ASSET_SLUGS = {
    "BTC": "btc", "ETH": "eth", "SOL": "sol",
    "XRP": "xrp", "DOGE": "doge", "BNB": "bnb",
}

# Timeframe → seconds
TIMEFRAME_SECONDS = {"5m": 300, "15m": 900}


class MarketDiscovery:
    """Discovers active Up/Down markets by probing exact Gamma API slugs."""

    def __init__(self, symbols: list[str] | None = None, timeframes: list[str] | None = None):
        self.symbols = symbols or ["BTC", "ETH", "SOL"]
        self.timeframes = timeframes or ["5m", "15m"]

    def _build_slugs(self, asset: str, timeframe: str) -> list[str]:
        """Build exact slugs for the current time window only.

        Probing prev (expired) and next (not yet created) windows wastes API calls
        and returns untradeable markets. We only want the active current window.
        """
        interval = TIMEFRAME_SECONDS.get(timeframe, 300)
        now = int(time.time())
        current_start = (now // interval) * interval

        asset_slug = ASSET_SLUGS.get(asset.upper(), asset.lower())
        return [f"{asset_slug}-updown-{timeframe}-{current_start}"]

    async def discover(self, client: httpx.AsyncClient | None = None) -> list[MarketState]:
        """Discover all matching Up/Down markets by probing exact slugs."""
        close_client = False
        if client is None:
            client = httpx.AsyncClient(timeout=30.0)
            close_client = True

        try:
            tasks = []
            for symbol in self.symbols:
                for tf in self.timeframes:
                    slugs = self._build_slugs(symbol, tf)
                    for slug in slugs:
                        tasks.append(self._fetch_by_slug(client, symbol, tf, slug))

            results = await asyncio.gather(*tasks, return_exceptions=True)

            markets: list[MarketState] = []
            seen_ids: set[str] = set()

            for result in results:
                if isinstance(result, MarketState) and result.market_id not in seen_ids:
                    seen_ids.add(result.market_id)
                    markets.append(result)
                elif isinstance(result, Exception):
                    logger.debug("Slug probe failed: %s", result)

            logger.info("Discovered %d Up/Down markets across %d symbols",
                        len(markets), len(self.symbols))
            return markets

        finally:
            if close_client:
                await client.aclose()

    async def _fetch_by_slug(
        self, client: httpx.AsyncClient, symbol: str, timeframe: str, slug: str
    ) -> Optional[MarketState]:
        """Fetch a single market by exact slug match."""
        url = f"{GAMMA_API_URL}/markets"
        params = {"slug": slug, "limit": "1"}

        try:
            resp = await client.get(url, params=params)
            if resp.status_code != 200:
                return None
            data = resp.json()
        except Exception:
            return None

        if not data or not isinstance(data, list) or len(data) == 0:
            return None

        raw = data[0]
        question = raw.get("question", "")

        # Verify it's an Up/Down market
        if "up or down" not in question.lower():
            return None

        # Verify it's active and not closed
        if not raw.get("active", False) or raw.get("closed", True):
            return None

        # Extract CLOB token IDs
        clob_tokens = raw.get("clobTokenIds", "[]")
        if isinstance(clob_tokens, str):
            try:
                clob_tokens = json.loads(clob_tokens)
            except json.JSONDecodeError:
                return None

        if not clob_tokens or len(clob_tokens) < 2:
            return None

        up_token_id = clob_tokens[0] if isinstance(clob_tokens[0], str) else clob_tokens[0].get("id", "")
        down_token_id = clob_tokens[1] if isinstance(clob_tokens[1], str) else clob_tokens[1].get("id", "")

        if not up_token_id or not down_token_id:
            return None

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

        # Derive start/end from slug timestamp (Gamma returns date-only strings)
        interval = TIMEFRAME_SECONDS.get(timeframe, 300)
        slug_parts = slug.split("-")
        try:
            slug_ts = int(slug_parts[-1])
            start_time = datetime.fromtimestamp(slug_ts, tz=timezone.utc)
            end_time = datetime.fromtimestamp(slug_ts + interval, tz=timezone.utc)
        except (ValueError, IndexError):
            start_time = datetime.min.replace(tzinfo=timezone.utc)
            end_time = datetime.max.replace(tzinfo=timezone.utc)

        return MarketState(
            market_id=str(raw.get("id", "")),
            question=question,
            slug=raw.get("slug", slug),
            symbol=symbol.upper(),
            timeframe=timeframe,
            up_token_id=up_token_id,
            down_token_id=down_token_id,
            price_to_beat=price_to_beat,
            start_time=start_time,
            end_time=end_time,
            active=raw.get("active", True),
            tick_size=float(raw.get("orderPriceMinTickSize", 0.01)),
        )
