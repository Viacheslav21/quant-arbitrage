import os
import time
import asyncio
import logging
from datetime import datetime, timezone
from dotenv import load_dotenv

from engine.scanner import PolymarketScanner
from engine.groups import assign
from engine.detector import Detector
from utils.db import Database
from utils.telegram import TelegramBot

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
log = logging.getLogger("main")

CONFIG = {
    "DATABASE_URL":    os.getenv("DATABASE_URL"),
    "TELEGRAM_TOKEN":  os.getenv("TELEGRAM_BOT_TOKEN"),
    "TELEGRAM_CHAT_ID": os.getenv("TELEGRAM_CHAT_ID"),
    "BANKROLL":        float(os.getenv("BANKROLL", "1000")),
    "SIMULATION":      os.getenv("SIMULATION", "true").lower() == "true",
    "SCAN_INTERVAL":   int(os.getenv("ARB_SCAN_INTERVAL", "4")),
    "FULL_SCAN_EVERY": int(os.getenv("ARB_FULL_SCAN_EVERY", "8")),  # full fetch every N ticks
    "TP_PCT":          float(os.getenv("ARB_TP_PCT", "0.08")),
    "SL_PCT":          float(os.getenv("ARB_SL_PCT", "0.05")),
    "TIMEOUT_MIN":     int(os.getenv("ARB_TIMEOUT_MIN", "30")),
    "MIN_VOLUME":      float(os.getenv("MIN_VOLUME", "50000")),
    "MAX_OPEN":        int(os.getenv("ARB_MAX_OPEN", "10")),
    "KELLY_FRAC":      float(os.getenv("ARB_KELLY_FRAC", "0.05")),
    "CONFIG_TAG":      os.getenv("ARB_CONFIG_TAG", "arb-v1"),
}


async def execute_signal(sig: dict, db: Database, telegram: TelegramBot, config: dict):
    """Open a position for an arbitrage signal."""
    open_pos = await db.get_open_positions(config["CONFIG_TAG"])
    if len(open_pos) >= config["MAX_OPEN"]:
        log.info(f"[EXEC] Max open ({config['MAX_OPEN']}), skipping {sig['question'][:40]}")
        return False

    # Check if already have position on this market
    for p in open_pos:
        if p["market_id"] == sig["market_id"]:
            return False

    stats = await db.get_stats()
    bankroll = stats.get("bankroll", config["BANKROLL"])
    stake = round(bankroll * config["KELLY_FRAC"], 2)
    if stake < 1.0:
        return False

    mode = "🧪 SIM" if config["SIMULATION"] else "💰 REAL"
    pos_id = f"arb_{sig['market_id'][:8]}_{int(time.time())}"
    sig_id = f"asig_{sig['market_id'][:8]}_{int(time.time())}"

    pos = {
        "id":         pos_id,
        "market_id":  sig["market_id"],
        "signal_id":  sig_id,
        "question":   sig["question"],
        "theme":      sig["group"],
        "side":       sig["side"],
        "side_price": sig["side_price"],
        "ev":         sig["ev"],
        "kelly":      config["KELLY_FRAC"],
        "stake_amt":  stake,
        "tp_pct":     config["TP_PCT"],
        "sl_pct":     config["SL_PCT"],
        "config_tag": config["CONFIG_TAG"],
    }

    sig["id"] = sig_id
    sig["executed"] = True
    await db.save_arb_signal(sig)
    await db.save_position(pos)

    log.info(f"[EXEC] {mode} {sig['side']} '{sig['question'][:50]}' | ${stake} EV:{sig['ev']*100:.1f}% group:{sig['group']}")
    await telegram.send(
        f"⚡ <b>ARB SIGNAL [{mode}]</b>\n\n"
        f"❓ {sig['question'][:150]}\n\n"
        f"{'✅ YES' if sig['side']=='YES' else '❌ NO'} по <b>{sig['side_price']*100:.1f}¢</b>\n\n"
        f"📊 EV:<b>+{sig['ev']*100:.1f}%</b> | Group:<b>{sig['group']}</b>\n"
        f"🔗 Leader: {sig['leader_q']} ({sig['leader_move']*100:+.1f}¢)\n"
        f"💵 Stake:<b>${stake}</b>"
    )
    return True


async def monitor_positions(db: Database, telegram: TelegramBot, scanner: PolymarketScanner,
                             config: dict, markets: list):
    """Monitor open arb positions for TP/SL/timeout."""
    open_pos = await db.get_open_positions(config["CONFIG_TAG"])
    if not open_pos:
        return

    market_map = {m["id"]: m for m in markets}
    now = time.time()

    for pos in open_pos:
        m = market_map.get(pos["market_id"])
        if not m:
            continue

        price = m["yes_price"] if pos["side"] == "YES" else (1 - m["yes_price"])
        upnl = (price / pos["side_price"] - 1) * pos["stake_amt"]
        await db.update_position_price(pos["id"], price, upnl)

        pnl_pct = (price - pos["side_price"]) / pos["side_price"]
        close_reason = None

        # Take profit
        if pnl_pct >= config["TP_PCT"]:
            close_reason = "TAKE_PROFIT"

        # Stop loss
        elif pnl_pct <= -config["SL_PCT"]:
            close_reason = "STOP_LOSS"

        # Timeout — 30 min max hold
        elif pos.get("opened_at"):
            age_min = (datetime.now(timezone.utc) - pos["opened_at"]).total_seconds() / 60
            if age_min >= config["TIMEOUT_MIN"]:
                close_reason = "TIMEOUT"

        if not close_reason:
            continue

        payout = round(pos["stake_amt"] * (1 + pnl_pct), 2)
        pnl = round(payout - pos["stake_amt"], 2)
        result = "WIN" if pnl > 0 else "LOSS"

        await db.close_position(pos["id"], result, pnl, payout, close_reason)
        await db.update_bankroll(pnl, result)

        emoji = "✅" if result == "WIN" else "❌"
        log.info(f"[CLOSE] {emoji} {close_reason} '{pos['question'][:40]}' PnL:{pnl:+.2f}")
        await telegram.send(
            f"{emoji} <b>ARB CLOSE — {close_reason}</b>\n\n"
            f"❓ {pos['question'][:120]}\n"
            f"💰 P&L:<b>{pnl:+.2f}$</b> ({pnl_pct*100:+.1f}%)\n"
            f"📊 {result} | Entry:{pos['side_price']*100:.1f}¢ → Exit:{price*100:.1f}¢"
        )


async def main():
    log.info(f"🚀 Quant Arbitrage starting | {'SIM 🧪' if CONFIG['SIMULATION'] else 'REAL 💰'}")

    db = Database(CONFIG["DATABASE_URL"])
    await db.init()

    scanner = PolymarketScanner(CONFIG)
    detector = Detector()
    telegram = TelegramBot(CONFIG["TELEGRAM_TOKEN"], CONFIG["TELEGRAM_CHAT_ID"])

    loop = asyncio.get_event_loop()
    for sig_name in ("SIGTERM", "SIGINT"):
        try:
            import signal as _signal
            loop.add_signal_handler(
                getattr(_signal, sig_name),
                lambda: asyncio.create_task(_shutdown(db, telegram, scanner)),
            )
        except (NotImplementedError, AttributeError):
            pass

    markets = []
    tick = 0

    while True:
        try:
            tick += 1
            now = time.time()

            # Full market fetch every N ticks (save API calls)
            if tick % CONFIG["FULL_SCAN_EVERY"] == 1 or not markets:
                markets = await scanner.fetch()
                if not markets:
                    await asyncio.sleep(CONFIG["SCAN_INTERVAL"])
                    continue

            # Assign markets to correlation groups
            groups = assign(markets)

            # Feed prices and detect signals
            total_signals = []
            for group_name, group_markets in groups.items():
                detector.update(group_markets)
                signals = detector.detect(group_name, group_markets)
                total_signals.extend(signals)

            # Execute signals
            for sig in total_signals[:3]:  # max 3 signals per tick
                executed = await execute_signal(sig, db, telegram, CONFIG)
                if executed:
                    detector.mark_cooldown(sig["market_id"])

            # Monitor open positions
            await monitor_positions(db, telegram, scanner, CONFIG, markets)

            # Stats logging every 30 ticks (~2 min)
            if tick % 30 == 0:
                open_pos = await db.get_open_positions(CONFIG["CONFIG_TAG"])
                log.info(f"[TICK #{tick}] {len(groups)} groups | {len(markets)} markets | {len(open_pos)} open arb positions")

        except Exception as e:
            log.error(f"[MAIN] {e}", exc_info=True)

        await asyncio.sleep(CONFIG["SCAN_INTERVAL"])


async def _shutdown(db, telegram, scanner):
    log.info("🛑 Shutting down...")
    await scanner.close()
    await telegram.close()
    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
