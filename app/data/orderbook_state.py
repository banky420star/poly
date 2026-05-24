from dataclasses import dataclass


@dataclass
class OrderBookState:
    token_id: str
    best_bid: float
    best_ask: float
    bid_depth: float = 0.0
    ask_depth: float = 0.0
    last_trade_price: float | None = None
    last_trade_side: str | None = None

    @property
    def spread(self) -> float:
        if self.best_bid <= 0 or self.best_ask <= 0:
            return 999.0
        return self.best_ask - self.best_bid
