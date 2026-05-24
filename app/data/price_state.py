from dataclasses import dataclass


@dataclass
class PriceState:
    symbol: str
    price: float
    timestamp_ms: int
    source: str
