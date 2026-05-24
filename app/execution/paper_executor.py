from app.agents.entry_timing_agent import EntryDecision
from app.execution.position_manager import Position, PositionManager


class PaperExecutor:
    def __init__(self, position_manager: PositionManager, bankroll_usd: float, max_position_pct: float):
        self.position_manager = position_manager
        self.bankroll_usd = bankroll_usd
        self.max_position_pct = max_position_pct
        self.closed_trades = []

    def execute_entry(self, market_id: str, decision: EntryDecision):
        if not decision.side or not decision.limit_price:
            return {"status": "NO_ENTRY"}

        size_usd = self.bankroll_usd * self.max_position_pct
        shares = size_usd / decision.limit_price

        position = Position(
            market_id=market_id,
            side=decision.side,
            avg_entry=decision.limit_price,
            shares=shares,
        )

        self.position_manager.open_position(position)

        return {
            "status": "PAPER_FILLED",
            "market_id": market_id,
            "side": decision.side,
            "price": decision.limit_price,
            "shares": shares,
            "size_usd": size_usd,
        }

    def execute_exit(self, market_id: str, exit_price: float, reason: str):
        position = self.position_manager.get(market_id)

        if not position:
            return {"status": "NO_POSITION"}

        pnl = (exit_price - position.avg_entry) * position.shares

        trade = {
            "market_id": market_id,
            "side": position.side,
            "entry_price": position.avg_entry,
            "exit_price": exit_price,
            "shares": position.shares,
            "pnl": pnl,
            "reason": reason,
        }

        self.closed_trades.append(trade)
        self.position_manager.close_position(market_id)

        return {
            "status": "PAPER_CLOSED",
            **trade,
        }
