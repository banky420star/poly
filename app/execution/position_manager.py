from dataclasses import dataclass


@dataclass
class Position:
    market_id: str
    side: str
    avg_entry: float
    shares: float


class PositionManager:
    def __init__(self):
        self.positions: dict[str, Position] = {}

    def get(self, market_id: str) -> Position | None:
        return self.positions.get(market_id)

    def open_position(self, position: Position):
        self.positions[position.market_id] = position

    def close_position(self, market_id: str):
        self.positions.pop(market_id, None)
