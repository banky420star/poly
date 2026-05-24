"""Performance metrics — win rate, calibration, PnL breakdowns."""
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger("poly.metrics")


@dataclass
class MetricsReport:
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    total_pnl: float = 0.0
    avg_pnl: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    max_drawdown: float = 0.0
    profit_factor: float = 0.0
    brier_score: float | None = None
    avg_entry_edge: float = 0.0
    avg_confidence: float = 0.0

    def print_report(self):
        print(f"\n  PERFORMANCE REPORT")
        print(f"  {'─' * 40}")
        print(f"  Trades:     {self.total_trades}")
        print(f"  Win Rate:   {self.win_rate:.1%} ({self.winning_trades}W / {self.losing_trades}L)")
        print(f"  Total PnL:  ${self.total_pnl:.2f}")
        print(f"  Avg PnL:    ${self.avg_pnl:.4f}")
        print(f"  Avg Win:    ${self.avg_win:.4f}")
        print(f"  Avg Loss:   ${self.avg_loss:.4f}")
        print(f"  Profit Factor: {self.profit_factor:.2f}")
        print(f"  Max Drawdown:  ${self.max_drawdown:.2f}")
        if self.brier_score is not None:
            print(f"  Brier Score:   {self.brier_score:.4f}")
        print(f"  Avg Edge:   {self.avg_entry_edge:.4f}")
        print(f"  Avg Confidence: {self.avg_confidence:.4f}")


class MetricsEngine:
    """Computes performance metrics from the database."""

    def __init__(self, db_path: str = "data/bot.db"):
        self.db_path = Path(db_path)

    async def compute(self) -> MetricsReport:
        """Read all closed trades and compute metrics.

        Returns a MetricsReport. If no database exists, returns empty report.
        """
        if not self.db_path.exists():
            logger.warning("No database found at %s", self.db_path)
            return MetricsReport()

        import aiosqlite

        report = MetricsReport()

        async with aiosqlite.connect(str(self.db_path)) as db:
            # Closed trades
            cursor = await db.execute(
                "SELECT pnl, entry_price, exit_price FROM paper_trades WHERE status = 'CLOSED'"
            )
            closed = await cursor.fetchall()

            if not closed:
                return report

            report.total_trades = len(closed)

            pnls = [row[0] for row in closed if row[0] is not None]
            wins = [p for p in pnls if p > 0]
            losses = [p for p in pnls if p <= 0]

            report.winning_trades = len(wins)
            report.losing_trades = len(losses)
            report.win_rate = report.winning_trades / report.total_trades if report.total_trades > 0 else 0.0
            report.total_pnl = sum(pnls)
            report.avg_pnl = report.total_pnl / report.total_trades if report.total_trades > 0 else 0.0
            report.avg_win = sum(wins) / len(wins) if wins else 0.0
            report.avg_loss = sum(losses) / len(losses) if losses else 0.0

            gross_profit = sum(wins) if wins else 0.0
            gross_loss = abs(sum(losses)) if losses else 1.0
            report.profit_factor = gross_profit / gross_loss if gross_loss > 0 else 0.0

            # Max drawdown from cumulative PnL
            cumulative = 0.0
            peak = 0.0
            max_dd = 0.0
            for pnl in pnls:
                cumulative += pnl
                peak = max(peak, cumulative)
                max_dd = max(max_dd, peak - cumulative)
            report.max_drawdown = max_dd

            # Average entry edge from signals table
            cursor = await db.execute(
                "SELECT AVG(adjusted_edge), AVG(confidence) FROM signals"
            )
            row = await cursor.fetchone()
            if row:
                report.avg_entry_edge = row[0] or 0.0
                report.avg_confidence = row[1] or 0.0

            # Brier score: (1/N) * sum((prob - outcome)^2)
            # outcome = 1 if PnL > 0, 0 otherwise
            cursor = await db.execute(
                """SELECT s.prob_up, t.pnl
                   FROM signals s
                   JOIN paper_trades t ON s.market_id = t.market_id
                   WHERE t.status = 'CLOSED'"""
            )
            rows = await cursor.fetchall()
            if rows:
                brier_sum = 0.0
                for prob_up, pnl in rows:
                    outcome = 1.0 if (pnl or 0) > 0 else 0.0
                    brier_sum += (prob_up - outcome) ** 2
                report.brier_score = brier_sum / len(rows)

        return report

    def compute_sync(self) -> MetricsReport:
        """Synchronous wrapper for compute()."""
        import asyncio
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.compute())
        else:
            import concurrent.futures
            future = asyncio.run_coroutine_threadsafe(self.compute(), loop)
            return future.result()
