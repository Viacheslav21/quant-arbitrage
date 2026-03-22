import logging
import asyncpg

log = logging.getLogger("db")


class Database:
    """DB layer for arbitrage bot.
    Reads shared quant-engine tables (positions, stats) for context.
    Writes exclusively to own tables (arb_positions, arb_signals, arb_stats)."""

    def __init__(self, url: str, starting_bankroll: float = 1000.0):
        self.url = url
        self.pool = None
        self.starting_bankroll = starting_bankroll

    async def init(self):
        self.pool = await asyncpg.create_pool(self.url, min_size=2, max_size=5, command_timeout=30)
        async with self.pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS arb_signals (
                    id TEXT PRIMARY KEY,
                    market_id TEXT,
                    question TEXT,
                    side TEXT,
                    side_price REAL,
                    ev REAL,
                    group_name TEXT,
                    leader_question TEXT,
                    leader_move REAL,
                    executed BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS arb_positions (
                    id TEXT PRIMARY KEY,
                    market_id TEXT,
                    signal_id TEXT,
                    question TEXT,
                    group_name TEXT,
                    side TEXT,
                    side_price REAL,
                    ev REAL,
                    kelly REAL,
                    stake_amt REAL,
                    current_price REAL,
                    unrealized_pnl REAL DEFAULT 0,
                    tp_pct REAL,
                    sl_pct REAL,
                    status TEXT DEFAULT 'open',
                    result TEXT,
                    pnl REAL,
                    payout REAL,
                    close_reason TEXT,
                    opened_at TIMESTAMPTZ DEFAULT NOW(),
                    closed_at TIMESTAMPTZ
                );
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS arb_stats (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    bankroll REAL DEFAULT 1000,
                    total_pnl REAL DEFAULT 0,
                    total_bets INTEGER DEFAULT 0,
                    wins INTEGER DEFAULT 0,
                    losses INTEGER DEFAULT 0,
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                );
            """)
            # Migrations
            await conn.execute("ALTER TABLE arb_signals ADD COLUMN IF NOT EXISTS signal_type TEXT DEFAULT 'leader_lagger'")
            # Ensure stats row exists
            await conn.execute("""
                INSERT INTO arb_stats (id, bankroll) VALUES (1, $1)
                ON CONFLICT (id) DO NOTHING
            """, self.starting_bankroll)
        log.info("[DB] Connected, arb tables ready")

    # ── Signals ──

    async def save_arb_signal(self, sig: dict):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO arb_signals (id, market_id, question, side, side_price, ev,
                    group_name, leader_question, leader_move, executed, signal_type)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                ON CONFLICT (id) DO NOTHING
            """, sig["id"], sig["market_id"], sig["question"], sig["side"],
                sig["side_price"], sig["ev"], sig["group"], sig["leader_q"],
                sig["leader_move"], sig.get("executed", False),
                sig.get("signal_type", "leader_lagger"))

    # ── Positions (own table) ──

    async def save_position(self, pos: dict):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO arb_positions (id, market_id, signal_id, question, group_name,
                    side, side_price, ev, kelly, stake_amt, current_price, tp_pct, sl_pct)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
            """, pos["id"], pos["market_id"], pos.get("signal_id"),
                pos["question"], pos.get("theme", "arb"), pos["side"],
                pos["side_price"], pos["ev"], pos["kelly"], pos["stake_amt"],
                pos["side_price"], pos["tp_pct"], pos["sl_pct"])

    async def get_open_positions(self, config_tag: str = None) -> list:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM arb_positions WHERE status='open' ORDER BY opened_at DESC")
            return [dict(r) for r in rows]

    async def update_position_price(self, pos_id: str, price: float, upnl: float):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE arb_positions SET current_price=$1, unrealized_pnl=$2 WHERE id=$3",
                price, upnl, pos_id)

    async def close_position(self, pos_id: str, result: str, pnl: float, payout: float,
                              reason: str, outcome: str = ""):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE arb_positions SET status='closed', result=$1, pnl=$2, payout=$3,
                    close_reason=$4, closed_at=NOW()
                WHERE id=$5
            """, result, pnl, payout, reason, pos_id)

    # ── Stats (own table) ──

    async def get_stats(self) -> dict:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM arb_stats WHERE id=1")
            if row:
                return dict(row)
            return {"bankroll": self.starting_bankroll, "total_pnl": 0, "wins": 0, "losses": 0}

    async def update_bankroll(self, pnl: float, result: str):
        async with self.pool.acquire() as conn:
            if result == "WIN":
                await conn.execute(
                    "UPDATE arb_stats SET bankroll=bankroll+$1, total_pnl=total_pnl+$1, total_bets=total_bets+1, wins=wins+1, updated_at=NOW() WHERE id=1",
                    pnl)
            else:
                await conn.execute(
                    "UPDATE arb_stats SET bankroll=bankroll+$1, total_pnl=total_pnl+$1, total_bets=total_bets+1, losses=losses+1, updated_at=NOW() WHERE id=1",
                    pnl)

    # ── Shared tables (read-only) ──

    async def get_shared_stats(self) -> dict:
        """Read stats from shared quant-engine table (read-only)."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM stats WHERE id=1")
            if row:
                return dict(row)
            return {}

    async def get_shared_open_positions(self) -> list:
        """Read open positions from shared quant-engine table (read-only)."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM positions WHERE status='open' ORDER BY opened_at DESC")
            return [dict(r) for r in rows]

    async def close(self):
        if self.pool:
            await self.pool.close()
