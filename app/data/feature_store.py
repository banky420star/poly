from dataclasses import dataclass
from datetime import datetime, timezone
from math import sqrt

import numpy as np

from app.data.market_state import MarketState
from app.data.orderbook_state import OrderBookState
from app.data.price_state import PriceState


@dataclass
class FeatureSnapshot:
    market_id: str
    symbol: str
    spot: float
    price_to_beat: float
    distance_to_target: float
    seconds_remaining: int
    velocity_30s: float
    volatility_60s: float
    up_bid: float
    up_ask: float
    down_bid: float
    down_ask: float
    up_spread: float
    down_spread: float


class FeatureStore:
    def __init__(self):
        self.price_history: dict[str, list[tuple[int, float]]] = {}

    def update_price(self, price_state: PriceState):
        history = self.price_history.setdefault(price_state.symbol, [])
        history.append((price_state.timestamp_ms, price_state.price))

        cutoff = price_state.timestamp_ms - 180_000
        self.price_history[price_state.symbol] = [
            row for row in history if row[0] >= cutoff
        ]

    def _velocity(self, symbol: str, window_ms: int = 30_000) -> float:
        history = self.price_history.get(symbol, [])
        if len(history) < 2:
            return 0.0

        latest_ts, latest_price = history[-1]
        cutoff = latest_ts - window_ms
        older = [row for row in history if row[0] <= cutoff]

        if not older:
            return 0.0

        old_ts, old_price = older[-1]
        seconds = max((latest_ts - old_ts) / 1000, 1)
        return (latest_price - old_price) / seconds

    def _volatility(self, symbol: str, window_ms: int = 60_000) -> float:
        history = self.price_history.get(symbol, [])
        if len(history) < 3:
            return 1.0

        latest_ts = history[-1][0]
        cutoff = latest_ts - window_ms
        prices = [price for ts, price in history if ts >= cutoff]

        if len(prices) < 3:
            return 1.0

        returns = np.diff(prices)
        return float(np.std(returns)) or 1.0

    def build(
        self,
        market: MarketState,
        spot: PriceState,
        up_book: OrderBookState,
        down_book: OrderBookState,
    ) -> FeatureSnapshot:
        self.update_price(spot)

        now = datetime.now(timezone.utc)
        seconds_remaining = max(int((market.end_time - now).total_seconds()), 0)

        return FeatureSnapshot(
            market_id=market.market_id,
            symbol=market.symbol,
            spot=spot.price,
            price_to_beat=market.price_to_beat,
            distance_to_target=spot.price - market.price_to_beat,
            seconds_remaining=seconds_remaining,
            velocity_30s=self._velocity(market.symbol),
            volatility_60s=self._volatility(market.symbol),
            up_bid=up_book.best_bid,
            up_ask=up_book.best_ask,
            down_bid=down_book.best_bid,
            down_ask=down_book.best_ask,
            up_spread=up_book.spread,
            down_spread=down_book.spread,
        )
