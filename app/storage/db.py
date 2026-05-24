"""SQLite storage for the paper odds bot — all decisions for calibration."""
import aiosqlite
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("poly.db")


DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
    market_id TEXT PRIMARY KEY,
    question TEXT,
    slug TEXT,
    symbol TEXT,
    timeframe TEXT,
    up_token_id TEXT,
    down_token_id TEXT,
    price_to_beat REAL,
    end_time TEXT,
    first_seen TEXT,
    last_seen TEXT,
    active INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    market_id TEXT NOT NULL,
    symbol TEXT,
    spot REAL,
    price_to_beat REAL,
    seconds_remaining INTEGER,
    prob_up REAL,
    prob_down REAL,
    confidence REAL,
    best_side TEXT,
    market_ask REAL,
    adjusted_edge REAL,
    max_entry_price REAL,
    required_edge REAL,
    decision TEXT,
    reason TEXT
);

CREATE TABLE IF NOT EXISTS paper_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opened_ts TEXT NOT NULL,
    closed_ts TEXT,
    market_id TEXT NOT NULL,
    symbol TEXT,
    side TEXT,
    entry_price REAL,
    exit_price REAL,
    shares REAL,
    size_usd REAL,
    pnl REAL,
    status TEXT DEFAULT 'OPEN',
    exit_reason TEXT
);

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    cash REAL,
    position_count INTEGER,
    closed_trades INTEGER,
    total_pnl REAL
);

CREATE INDEX IF NOT EXISTS idx_signals_market ON signals(market_id);
CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts);
CREATE INDEX IF NOT EXISTS idx_trades_market ON paper_trades(market_id);
CREATE INDEX IF NOT EXISTS idx_trades_status ON paper_trades(status);
"""


class Database:
    """Async SQLite database for the bot."""

    def __init__(self, db_path: str = "data/bot.db"):
        self.db_path = Path(db_path)
        self.conn: aiosqlite.Connection | None = None

    async def start(self):
        """Open connection and create tables."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(str(self.db_path))
        await self.conn.executescript(DB_SCHEMA)
        await self.conn.commit()
        logger.info("Database ready: %s", self.db_path)

    async def close(self):
        if self.conn:
            await self.conn.close()
            self.conn = None

    async def record_signal(self, payload: dict):
        """Store a decision signal."""
        now = datetime.now(timezone.utc).isoformat()
        await self.conn.execute(
            """INSERT INTO signals (ts, market_id, symbol, spot, price_to_beat,
               seconds_remaining, prob_up, prob_down, confidence, best_side,
               market_ask, adjusted_edge, max_entry_price, required_edge,
               decision, reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                now,
                payload.get("market_id", ""),
                payload.get("symbol", ""),
                payload.get("spot"),
                payload.get("price_to_beat"),
                payload.get("seconds_remaining"),
                payload.get("prob_up"),
                payload.get("prob_down"),
                payload.get("confidence"),
                payload.get("best_side"),
                payload.get("market_ask"),
                payload.get("adjusted_edge"),
                payload.get("max_entry_price"),
                payload.get("required_edge"),
                payload.get("decision", ""),
                json.dumps(payload.get("reason", [])),
            ),
        )
        await self.conn.commit()

    async def record_entry(self, market_id: str, symbol: str, side: str,
                          entry_price: float, shares: float, size_usd: float):
        """Record a new paper trade."""
        now = datetime.now(timezone.utc).isoformat()
        await self.conn.execute(
            """INSERT INTO paper_trades (opened_ts, market_id, symbol, side,
               entry_price, shares, size_usd, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'OPEN')""",
            (now, market_id, symbol, side, entry_price, shares, size_usd),
        )
        await self.conn.commit()

    async def record_exit(self, market_id: str, exit_price: float, pnl: float, reason: str):
        """Close an open trade by market_id."""
        now = datetime.now(timezone.utc).isoformat()
        await self.conn.execute(
            """UPDATE paper_trades
               SET closed_ts = ?, exit_price = ?, pnl = ?, status = 'CLOSED', exit_reason = ?
               WHERE market_id = ? AND status = 'OPEN'""",
            (now, exit_price, pnl, reason, market_id),
        )
        await self.conn.commit()

    async def record_snapshot(self, cash: float, position_count: int,
                             closed_trades: int, total_pnl: float):
        """Record a portfolio snapshot."""
        now = datetime.now(timezone.utc).isoformat()
        await self.conn.execute(
            """INSERT INTO snapshots (ts, cash, position_count, closed_trades, total_pnl)
               VALUES (?, ?, ?, ?, ?)""",
            (now, cash, position_count, closed_trades, total_pnl),
        )
        await self.conn.commit()

    async def upsert_market(self, market):
        """Insert or update a market in the markets table."""
        now = datetime.now(timezone.utc).isoformat()
        existing = await self.conn.execute(
            "SELECT market_id FROM markets WHERE market_id = ?", (market.market_id,)
        )
        row = await existing.fetchone()

        if row:
            await self.conn.execute(
                """UPDATE markets SET last_seen = ?, active = ?, question = ?, slug = ?
                   WHERE market_id = ?""",
                (now, 1 if market.active else 0, market.question, market.slug, market.market_id),
            )
        else:
            await self.conn.execute(
                """INSERT INTO markets (market_id, question, slug, symbol, timeframe,
                   up_token_id, down_token_id, price_to_beat, end_time, first_seen, last_seen, active)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    market.market_id,
                    market.question,
                    market.slug,
                    market.symbol,
                    market.timeframe,
                    market.up_token_id,
                    market.down_token_id,
                    market.price_to_beat,
                    market.end_time.isoformat() if market.end_time else None,
                    now, now,
                    1 if market.active else 0,
                ),
            )
        await self.conn.commit()
