import os
import time
import asyncio
import logging
from datetime import datetime, timezone
from dotenv import load_dotenv

from engine.scanner import PolymarketScanner
from engine.groups import assign
from engine.detector import Detector
from engine.mispricing import MispricingDetector
from engine.ws_client import PolymarketWS
from utils.db import Database

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
    "TP_PCT":          float(os.getenv("ARB_TP_PCT", "0.10")),
    "SL_PCT":          float(os.getenv("ARB_SL_PCT", "0.04")),
    "TIMEOUT_MIN":     int(os.getenv("ARB_TIMEOUT_MIN", "30")),
    "MIN_VOLUME":      float(os.getenv("MIN_VOLUME", "50000")),
    "MAX_OPEN":        int(os.getenv("ARB_MAX_OPEN", "10")),
    "KELLY_FRAC":      float(os.getenv("ARB_KELLY_FRAC", "0.05")),
    "CONFIG_TAG":      os.getenv("ARB_CONFIG_TAG", "arb-v1"),
}


async def execute_signal(sig: dict, db: Database, config: dict):
    """Open a position for an arbitrage signal."""
    open_pos = await db.get_open_positions(config["CONFIG_TAG"])
    if len(open_pos) >= config["MAX_OPEN"]:
        log.info(f"[EXEC] Max open ({config['MAX_OPEN']}), skipping {sig['question'][:40]}")
        return False

    # Check if already have position on this market
    for p in open_pos:
        if p["market_id"] == sig["market_id"]:
            log.debug(f"[EXEC] skip '{sig['question'][:35]}': already have open position")
            return False

    stats = await db.get_stats()
    bankroll = stats.get("bankroll", config["BANKROLL"])
    stake = round(bankroll * config["KELLY_FRAC"], 2)
    stake = min(stake, 50.0)  # hard cap $50 per arb trade
    if stake < 1.0:
        log.warning(f"[EXEC] stake ${stake:.2f} too low (bankroll: ${bankroll:.2f}), skipping")
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

    sig_type = sig.get("signal_type", "leader_lagger")
    if sig_type == "mispricing":
        log.info(
            f"[EXEC] {mode} {sig['side']} '{sig['question'][:50]}' | ${stake} "
            f"EV:{sig['ev']*100:.1f}% conf:{sig.get('confidence',0):.2f} "
            f"type:MISPRICING | {sig.get('leader_q','')}"
        )
    else:
        log.info(
            f"[EXEC] {mode} {sig['side']} '{sig['question'][:50]}' | ${stake} "
            f"EV:{sig['ev']*100:.1f}% ρ:{sig.get('correlation',0):.2f} "
            f"conf:{sig.get('confidence',0):.2f} HL:{sig.get('half_life_m','?')}m "
            f"group:{sig['group']}"
        )
    return True


async def monitor_positions(db: Database, scanner: PolymarketScanner,
                             config: dict, markets: list,
                             pos_cache: dict = None, closing_ids: set = None):
    """Monitor open arb positions for TP/SL/timeout.
    Also syncs pos_cache for reactive WS-based TP/SL checks."""
    open_pos = await db.get_open_positions(config["CONFIG_TAG"])

    # Sync local position cache for reactive WS TP/SL
    if pos_cache is not None:
        pos_cache.clear()
        for p in open_pos:
            if closing_ids and p["id"] in closing_ids:
                continue
            pos_cache[p["market_id"]] = p

    if not open_pos:
        return

    market_map = {m["id"]: m for m in markets}
    now = time.time()

    for pos in open_pos:
        # Skip if already closed by reactive WS callback
        if closing_ids and pos["id"] in closing_ids:
            continue

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

        # PnL based on real market price (like a real trade)
        payout = round(pos["stake_amt"] * (1 + pnl_pct), 2)
        pnl = round(payout - pos["stake_amt"], 2)
        result = "WIN" if pnl > 0 else "LOSS"

        await db.close_position(pos["id"], result, pnl, payout, close_reason, exit_price=price)
        await db.update_bankroll(pnl, result)

        # Remove from cache since it's now closed
        if pos_cache is not None:
            pos_cache.pop(pos["market_id"], None)

        emoji = "✅" if result == "WIN" else "❌"
        age_str = ""
        if pos.get("opened_at"):
            age_m = (datetime.now(timezone.utc) - pos["opened_at"]).total_seconds() / 60
            age_str = f" age:{age_m:.0f}m"
        log.info(f"[CLOSE] {emoji} {close_reason} '{pos['question'][:40]}' PnL:{pnl:+.2f} entry:{pos['side_price']:.4f}→{price:.4f} ({pnl_pct*100:+.1f}%){age_str}")


async def main():
    log.info(f"🚀 Quant Arbitrage starting | {'SIM 🧪' if CONFIG['SIMULATION'] else 'REAL 💰'}")
    log.info(f"[CONFIG] TP:{CONFIG['TP_PCT']*100:.0f}% SL:{CONFIG['SL_PCT']*100:.0f}% "
             f"timeout:{CONFIG['TIMEOUT_MIN']}m kelly:{CONFIG['KELLY_FRAC']*100:.0f}% "
             f"max_open:{CONFIG['MAX_OPEN']} scan:{CONFIG['SCAN_INTERVAL']}s "
             f"bankroll:${CONFIG['BANKROLL']:.0f}")

    db = Database(CONFIG["DATABASE_URL"], starting_bankroll=CONFIG["BANKROLL"])
    await db.init()

    scanner = PolymarketScanner(CONFIG)
    detector = Detector()
    mispricing = MispricingDetector()
    ws = PolymarketWS()

    # Shared state between WS callbacks and main loop
    _pending_signals = []
    _open_positions = {}   # market_id -> position dict (local cache for reactive TP/SL)
    _closing_ids = set()   # position IDs being closed (prevent double-close)

    async def on_price_change(market_id, old_price, new_price):
        """Called by WebSocket on every price update — feed detector and check TP/SL reactively."""
        if market_id in market_map:
            market_map[market_id]["yes_price"] = new_price

        # Reactive TP/SL check on every price tick
        pos = _open_positions.get(market_id)
        if not pos or pos["id"] in _closing_ids:
            return

        price = new_price if pos["side"] == "YES" else (1 - new_price)
        pnl_pct = (price - pos["side_price"]) / pos["side_price"]

        close_reason = None
        if pnl_pct >= CONFIG["TP_PCT"]:
            close_reason = "TAKE_PROFIT"
        elif pnl_pct <= -CONFIG["SL_PCT"]:
            close_reason = "STOP_LOSS"

        if not close_reason:
            return

        _closing_ids.add(pos["id"])
        payout = round(pos["stake_amt"] * (1 + pnl_pct), 2)
        pnl = round(payout - pos["stake_amt"], 2)
        result = "WIN" if pnl > 0 else "LOSS"

        await db.close_position(pos["id"], result, pnl, payout, close_reason, exit_price=price)
        await db.update_bankroll(pnl, result)

        _open_positions.pop(market_id, None)
        _closing_ids.discard(pos["id"])

        emoji = "✅" if result == "WIN" else "❌"
        age_str = ""
        if pos.get("opened_at"):
            age_m = (datetime.now(timezone.utc) - pos["opened_at"]).total_seconds() / 60
            age_str = f" age:{age_m:.0f}m"
        log.info(f"[CLOSE] {emoji} {close_reason} '{pos['question'][:40]}' PnL:{pnl:+.2f} entry:{pos['side_price']:.4f}→{price:.4f} ({pnl_pct*100:+.1f}%){age_str}")

    async def on_trade(market_id, price, size, side):
        """Called on every trade — log whale trades."""
        if size >= 500:
            q = market_map.get(market_id, {}).get("question", "?")[:50]
            log.info(f"[WHALE] 🐋 ${size:.0f} {side} on '{q}' @ {price:.4f}")

    ws.set_callbacks(on_price_change=on_price_change, on_trade=on_trade)

    loop = asyncio.get_event_loop()
    for sig_name in ("SIGTERM", "SIGINT"):
        try:
            import signal as _signal
            loop.add_signal_handler(
                getattr(_signal, sig_name),
                lambda: asyncio.create_task(_shutdown(db, scanner, ws)),
            )
        except (NotImplementedError, AttributeError):
            pass

    # Initial full scan to discover markets and token IDs
    markets = await scanner.fetch()
    market_map = {m["id"]: m for m in markets}
    groups = assign(markets)
    grouped_ids = set()
    grouped_markets = []
    for gm in groups.values():
        for m in gm:
            grouped_ids.add(m["id"])
            grouped_markets.append(market_map.get(m["id"], m))

    log.info(f"[INIT] {len(markets)} markets, {len(grouped_ids)} in {len(groups)} groups")

    # Register grouped markets for WebSocket
    ws.register_markets(grouped_markets)

    # Start WebSocket in background
    ws_task = asyncio.create_task(ws.connect())

    # ── Task 1: Fast position monitor (every 1s) ────────────────────
    async def monitor_loop():
        """High-frequency TP/SL/timeout check — runs independently of detection."""
        while True:
            try:
                current_markets = list(market_map.values())
                await monitor_positions(db, scanner, CONFIG, current_markets,
                                        pos_cache=_open_positions, closing_ids=_closing_ids)
            except Exception as e:
                log.error(f"[MONITOR] {e}", exc_info=True)
            await asyncio.sleep(1)

    # ── Task 2: Detection + execution (every SCAN_INTERVAL) ─────────
    async def detect_loop():
        """Signal detection and trade execution."""
        tick = 0
        while True:
            try:
                tick += 1

                # Update market prices from WebSocket data
                for mid in grouped_ids:
                    ws_price = ws.get_price(mid)
                    if ws_price > 0 and mid in market_map:
                        market_map[mid]["yes_price"] = ws_price

                # Rebuild markets list with latest prices
                current_markets = list(market_map.values())
                groups = assign(current_markets)

                # Feed prices and detect signals
                detector._tick_count += 1
                total_signals = []
                for group_name, group_markets in groups.items():
                    detector.update(group_markets)
                    signals = detector.detect(group_name, group_markets)
                    total_signals.extend(signals)

                # Mispricing detection (runs on all markets, not just grouped)
                mp_signals = mispricing.detect(current_markets)
                total_signals.extend(mp_signals)

                # Sort: mispricing first (structural edge), then by confidence × EV
                total_signals.sort(
                    key=lambda s: (s.get("signal_type") == "mispricing", s.get("confidence", 0) * s["ev"]),
                    reverse=True,
                )

                mp_count = sum(1 for s in total_signals if s.get("signal_type") == "mispricing")
                ll_count = len(total_signals) - mp_count
                if total_signals:
                    log.debug(f"[SIGNALS] {len(total_signals)} total ({mp_count} mispricing, {ll_count} leader/lagger)")

                # Execute max 1 signal per tick — quality over quantity
                for sig in total_signals[:1]:
                    executed = await execute_signal(sig, db, CONFIG)
                    if executed:
                        if sig.get("signal_type") == "mispricing":
                            mispricing.mark_cooldown(sig["market_id"])
                        else:
                            detector.mark_cooldown(sig["market_id"], sig.get("group"))

                # Stats logging every 60 ticks (~4 min)
                if tick % 60 == 0:
                    open_pos = await db.get_open_positions(CONFIG["CONFIG_TAG"])
                    ws_active = len([1 for p in ws.prices.values() if time.time() - p.get("last_update", 0) < 30])
                    stats = await db.get_stats()
                    bankroll = stats.get("bankroll", CONFIG["BANKROLL"])
                    total_pnl = stats.get("total_pnl", 0)
                    wins = stats.get("wins", 0)
                    losses = stats.get("losses", 0)
                    total_bets = stats.get("total_bets", 0)
                    wr = f"{wins/(wins+losses)*100:.0f}%" if (wins+losses) > 0 else "n/a"
                    upnl = sum(p.get("unrealized_pnl", 0) for p in open_pos)
                    ready_stats = sum(1 for s in detector.stats.values() if s.ready)
                    log.info(
                        f"[TICK #{tick}] {len(groups)} groups | {ws_active} WS live | "
                        f"{len(open_pos)} open (uPnL:{upnl:+.2f}) | "
                        f"bankroll:${bankroll:.2f} PnL:${total_pnl:+.2f} | "
                        f"W/L:{wins}/{losses} ({wr}) bets:{total_bets} | "
                        f"stats ready:{ready_stats}/{len(detector.stats)}"
                    )
                    # Log individual open positions
                    for p in open_pos:
                        age_m = 0
                        if p.get("opened_at"):
                            age_m = (datetime.now(timezone.utc) - p["opened_at"]).total_seconds() / 60
                        log.info(
                            f"  📊 {p['side']} '{p['question'][:40]}' "
                            f"entry:{p['side_price']:.4f} now:{p.get('current_price',0):.4f} "
                            f"uPnL:{p.get('unrealized_pnl',0):+.2f} age:{age_m:.0f}m"
                        )

            except Exception as e:
                log.error(f"[DETECT] {e}", exc_info=True)

            await asyncio.sleep(CONFIG["SCAN_INTERVAL"])

    # ── Task 3: Market rescan (every ~10 min) ───────────────────────
    async def rescan_loop():
        """Periodic full market rescan to discover new markets."""
        rescan_interval = CONFIG["SCAN_INTERVAL"] * 150  # ~10 min
        while True:
            await asyncio.sleep(rescan_interval)
            try:
                new_markets = await scanner.fetch()
                if new_markets:
                    markets[:] = new_markets
                    market_map.clear()
                    market_map.update({m["id"]: m for m in markets})
                    groups = assign(markets)
                    new_grouped = []
                    for gm in groups.values():
                        for m in gm:
                            if m["id"] not in grouped_ids:
                                grouped_ids.add(m["id"])
                                new_grouped.append(market_map.get(m["id"], m))
                    if new_grouped:
                        ws.register_markets(new_grouped)
                        await ws.add_subscriptions(
                            [m["yes_token"] for m in new_grouped if m.get("yes_token")] +
                            [m["no_token"] for m in new_grouped if m.get("no_token")]
                        )
                        log.info(f"[RESCAN] +{len(new_grouped)} new markets added to WS")
                    log.info(f"[RESCAN] {len(markets)} markets, {len(grouped_ids)} in {len(groups)} groups")
            except Exception as e:
                log.error(f"[RESCAN] {e}", exc_info=True)

    # Launch all tasks concurrently
    await asyncio.gather(
        ws_task,
        monitor_loop(),
        detect_loop(),
        rescan_loop(),
    )


async def _shutdown(db, scanner, ws=None):
    log.info("🛑 Shutting down...")
    if ws:
        ws.stop()
    await scanner.close()
    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
