# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Polymarket dual-engine arbitrage bot. Two independent signal engines: (1) leader/lagger statistical arbitrage using z-scores, Pearson correlation, and Ornstein-Uhlenbeck mean reversion; (2) structural mispricing detection via strike/date monotonicity violations. Runs as a long-lived async Python process with WebSocket streaming and PostgreSQL persistence.

## Running

```bash
pip install -r requirements.txt
python main.py
```

Deployed via Procfile (`web: python main.py`). No test suite or linter is configured.

## Required Environment Variables

Set in `.env` (loaded via python-dotenv):
- `DATABASE_URL` — PostgreSQL connection string (shared with quant-engine)
- `SIMULATION` — "true"/"false", controls live vs simulated execution
- `BANKROLL` — starting capital (default 1000)
- Tuning: `ARB_SCAN_INTERVAL`, `ARB_FULL_SCAN_EVERY`, `ARB_TP_PCT`, `ARB_SL_PCT`, `ARB_TIMEOUT_MIN`, `MIN_VOLUME`, `ARB_MAX_OPEN`, `ARB_KELLY_FRAC`, `ARB_CONFIG_TAG`

Note: `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are loaded in CONFIG but Telegram integration is currently disabled (not wired into main loop).

## Architecture

**main.py** (~301 lines) — Event loop: scan → group → detect → execute → monitor positions. Runs on a configurable tick interval (default 4s). Full market rescan every 150 ticks (~10 min) to discover new markets. Max 1 signal executed per tick. Mispricing signals always prioritized over leader/lagger. WebSocket provides real-time prices between rescans.

**engine/scanner.py** (~73 lines) — `PolymarketScanner` fetches active markets from the Gamma API (`gamma-api.polymarket.com`), filtering by volume (≥50k) and liquidity (≥5k). Paginates up to 500 markets. Extracts YES/NO token IDs for WebSocket subscription. Price bounds: 3-97¢.

**engine/groups.py** (~56 lines) — `assign()` bins markets into correlation groups (oil, btc, eth, trump, iran, ukraine, israel, fed, gold, sp500) via keyword matching on the market question. Supports inverse keywords (e.g. "dip", "below" → direction=-1). A market belongs to at most one group; groups with <2 markets are discarded.

**engine/detector.py** (~306 lines) — `Detector` — leader/lagger statistical signal engine:
- **MarketStats**: per-market rolling statistics (prices, returns, volatility, z-scores) over 500-tick window (~33 min). Minimum 100 ticks (~7 min) before stats are ready.
- **Z-score detection**: leader needs |z| ≥ 2.0 (2-sigma move), lagger must have |z| < 0.5 (quiet). Lookback: 8 ticks (~32s).
- **Pearson correlation**: rolling ρ between market return series, minimum |ρ| ≥ 0.35. Effective correlation adjusted by direction: `eff_corr = raw_corr × leader_dir × lagger_dir`.
- **Ornstein-Uhlenbeck half-life**: OLS regression on pair spread (ΔS = α + β·S), minimum 50 data points. Half-life = -ln(2)/ln(1+β). Skips pairs where half-life exceeds 30-min timeout (450 ticks).
- **Expected move**: `|leader_move| × |eff_corr| × (1 − e^(−ln2 × hold/HL))` where hold = min(timeout, 3×HL).
- **Cost model**: spread/2 + 0.5¢ slippage subtracted from expected move.
- **EV formula**: `net_move / entry_price`. Range: 5%–15%.
- **Composite confidence**: 40% correlation + 30% z-significance (z/3.0, cap 1.0) + 20% liquidity (normalized to $50k) + 10% OU decay. Floor: 0.30.
- **Group size cap**: max 15 markets per group, sorted by liquidity.
- Signals ranked by `confidence × EV`.

**engine/mispricing.py** (~298 lines) — `MispricingDetector` — structural mispricing detection (mathematical edge from logical constraint violations):
- Parses market questions to extract (asset, direction, strike, date). Supports: btc, eth, oil, gold, sp500.
- **Strike monotonicity**: P("reach $90k") ≥ P("reach $100k") for same date. Higher strike = lower probability. Violation ≥ 3¢ = signal.
- **Date monotonicity**: P("by March") ≤ P("by December") for same strike. More time = higher probability. Violation ≥ 3¢ = signal.
- Generates paired signals: YES on underpriced + NO on overpriced (executes one side per tick).
- **EV**: (gap/2 − spread/2 − 0.5¢ slippage) / entry_price. Range: 2%–25%.
- **Confidence**: 50% violation size (gap/10¢) + 30% liquidity (norm $30k) + 20% volume (norm $100k). Floor: 0.25.
- Deduplication: if same market in multiple violations, keeps highest EV.
- Per-market cooldown: 10 min.
- Mispricing signals are always prioritized over leader/lagger in execution.

**engine/ws_client.py** (~244 lines) — `PolymarketWS` connects to `wss://ws-subscriptions-clob.polymarket.com/ws/market`. Handles price_change, last_trade_price, and book events. Correctly disambiguates YES/NO tokens and converts all prices to YES-denominated. Tracks spread, bid/ask. Callbacks for price changes and whale trades (≥$500). Auto-reconnects (5s delay), heartbeat (10s). Batch subscriptions (100 tokens). Dynamic add_subscriptions without reconnect.

**utils/db.py** (~168 lines) — `Database` wraps asyncpg pool. **Writes only to own tables** (`arb_signals`, `arb_positions`, `arb_stats`). Reads shared quant-engine tables (`positions`, `stats`) via `get_shared_*()` methods (read-only). Position lifecycle: open → monitor (update price/uPnL) → close (TP/SL/timeout). Bankroll tracked independently in `arb_stats`.

**utils/telegram.py** (~26 lines) — `TelegramBot` exists but is not currently wired into the main loop.

## Database Tables (owned by this bot)

| Table | Purpose |
|---|---|
| `arb_signals` | Generated signals: market_id, side, ev, group_name, leader info, signal_type (leader_lagger/mispricing) |
| `arb_positions` | Arb trades: side, stake, ev, kelly, tp/sl, status, result, pnl, close_reason |
| `arb_stats` | Singleton (id=1): bankroll, total_pnl, wins, losses |

Shared tables read (not written): `positions`, `stats` from quant-engine.

## Key Design Decisions

- **Dual signal engine**: leader/lagger (statistical) + mispricing (structural). Mispricing always prioritized.
- **Full isolation from quant-engine**: writes only to `arb_*` tables, never touches shared `positions`/`stats`.
- Position sizing: fixed Kelly fraction (default 5%) of arb bankroll, hard cap $50/trade.
- Positions auto-close on take-profit (8%), stop-loss (5%), or timeout (30 min).
- TP capping: realized PnL capped at TP% to avoid stale-price phantom gains.
- Max 1 signal per tick, max 10 open positions total.
- Each market can only have one open position at a time.
- Per-market cooldown 5 min, per-group cooldown 10 min (leader/lagger). Per-market cooldown 10 min (mispricing).
- Warmup period: 30 ticks (~2 min) before detection begins.
- Cost model: half-spread + 0.5¢ slippage deducted from every EV calculation.
