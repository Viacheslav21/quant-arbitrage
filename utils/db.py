import logging
import asyncpg

log = logging.getLogger("db")


class Database:
    """Thin DB layer — connects to shared quant-engine PostgreSQL.
    Only methods needed for arbitrage bot."""

    def __init__(self, url: str):
        self.url = url
        self.pool = None

    async def init(self):
        self.pool = await asyncpg.create_pool(self.url, min_size=2, max_size=5, command_timeout=30)
        # Ensure arb_signals table exists (separate from main signals table)
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
        log.info("[DB] Connected to shared database")

    async def save_arb_signal(self, sig: dict):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO arb_signals (id, market_id, question, side, side_price, ev,
                    group_name, leader_question, leader_move, executed)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                ON CONFLICT (id) DO NOTHING
            """, sig["id"], sig["market_id"], sig["question"], sig["side"],
                sig["side_price"], sig["ev"], sig["group"], sig["leader_q"],
                sig["leader_move"], sig.get("executed", False))

    async def save_position(self, pos: dict):
        """Save position to shared positions table."""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO positions (id, market_id, signal_id, question, theme, side,
                    side_price, p_final, ev, kl, kelly, stake_amt, current_price, url,
                    tp_pct, sl_pct, config_tag)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17)
            """, pos["id"], pos["market_id"], pos.get("signal_id"),
                pos["question"], pos.get("theme", "arb"), pos["side"],
                pos["side_price"], pos.get("p_final", 0.5), pos["ev"],
                0.0, pos["kelly"], pos["stake_amt"], pos["side_price"],
                "", pos["tp_pct"], pos["sl_pct"], pos["config_tag"])

    async def get_open_positions(self, config_tag: str = None) -> list:
        async with self.pool.acquire() as conn:
            if config_tag:
                rows = await conn.fetch(
                    "SELECT * FROM positions WHERE status='open' AND config_tag=$1 ORDER BY opened_at DESC",
                    config_tag)
            else:
                rows = await conn.fetch(
                    "SELECT * FROM positions WHERE status='open' ORDER BY opened_at DESC")
            return [dict(r) for r in rows]

    async def update_position_price(self, pos_id: str, price: float, upnl: float):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE positions SET current_price=$1, unrealized_pnl=$2 WHERE id=$3",
                price, upnl, pos_id)

    async def close_position(self, pos_id: str, result: str, pnl: float, payout: float,
                              reason: str, outcome: str = ""):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE positions SET status='closed', result=$1, pnl=$2, payout=$3,
                    outcome=$4, closed_at=NOW()
                WHERE id=$5
            """, result, pnl, payout, outcome or reason, pos_id)

    async def get_stats(self) -> dict:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM stats WHERE id=1")
            if row:
                return dict(row)
            return {"bankroll": 1000, "total_pnl": 0, "wins": 0, "losses": 0}

    async def update_bankroll(self, pnl: float, result: str):
        async with self.pool.acquire() as conn:
            if result == "WIN":
                await conn.execute(
                    "UPDATE stats SET bankroll=bankroll+$1, total_pnl=total_pnl+$1, total_bets=total_bets+1, wins=wins+1, updated_at=NOW() WHERE id=1",
                    pnl)
            else:
                await conn.execute(
                    "UPDATE stats SET bankroll=bankroll+$1, total_pnl=total_pnl+$1, total_bets=total_bets+1, losses=losses+1, updated_at=NOW() WHERE id=1",
                    pnl)

    async def close(self):
        if self.pool:
            await self.pool.close()
