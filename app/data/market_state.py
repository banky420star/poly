from dataclasses import dataclass
from datetime import datetime


@dataclass
class MarketState:
    market_id: str
    question: str
    slug: str
    symbol: str
    timeframe: str
    up_token_id: str
    down_token_id: str
    price_to_beat: float
    start_time: datetime
    end_time: datetime
    active: bool = True
    tick_size: float = 0.01
