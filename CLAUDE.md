# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Polymarket arbitrage bot that detects leader/lagger price divergences across correlated prediction markets via WebSocket streaming. Uses statistical models (z-scores, Pearson correlation, Ornstein-Uhlenbeck mean reversion) for signal generation. Runs as a long-lived async Python process with PostgreSQL persistence.

## Running

```bash
pip install -r requirements.txt
python main.py
```

Deployed via Procfile (`web: python main.py`). No test suite or linter is configured.

## Required Environment Variables

Set in `.env` (loaded via python-dotenv):
- `DATABASE_URL` — PostgreSQL connection string (shared with a broader quant-engine system)
- `SIMULATION` — "true"/"false", controls live vs simulated execution
- `BANKROLL` — starting capital (default 1000)
- Tuning: `ARB_SCAN_INTERVAL`, `ARB_FULL_SCAN_EVERY`, `ARB_TP_PCT`, `ARB_SL_PCT`, `ARB_TIMEOUT_MIN`, `MIN_VOLUME`, `ARB_MAX_OPEN`, `ARB_KELLY_FRAC`, `ARB_CONFIG_TAG`

Note: `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are still loaded in CONFIG but Telegram integration is currently disabled (no alerts sent).

## Architecture

**main.py** — Event loop: scan → group → detect → execute → monitor positions. Runs on a configurable tick interval (default 4s). Full market rescan every 150 ticks (~10 min) to discover new markets. Max 1 signal executed per tick. WebSocket provides real-time prices between rescans.

**engine/scanner.py** — `PolymarketScanner` fetches active markets from the Gamma API (`gamma-api.polymarket.com`), filtering by volume (≥50k) and liquidity (≥5k). Paginates up to 500 markets. Extracts YES/NO token IDs for WebSocket subscription.

**engine/groups.py** — `assign()` bins markets into correlation groups (oil, btc, eth, trump, iran, ukraine, israel, fed, gold, sp500) via keyword matching on the market question. Supports inverse keywords (e.g. "dip", "below" → direction=-1). A market belongs to at most one group; groups with <2 markets are discarded.

**engine/detector.py** — `Detector` — leader/lagger statistical signal engine. Core models:
- **MarketStats**: per-market rolling statistics (prices, returns, volatility, z-scores) over 500-tick window (~33 min). Minimum 100 ticks (~7 min) before stats are ready
- **Z-score detection**: leader needs |z| ≥ 2.0 (statistically significant move), lagger must have |z| < 0.5
- **Pearson correlation**: rolling ρ between market return series, minimum ρ ≥ 0.35 to consider a pair
- **Ornstein-Uhlenbeck half-life**: OLS regression on pair spread (ΔS = α + β·S), minimum 50 data points. Skips pairs where half-life exceeds timeout
- **EV formula**: `expected_move = |leader_move| × |ρ| × (1 − e^(−ln2 × hold/HL)) − spread/2 − slippage(0.5¢)`, then `ev = expected_move / entry_price`
- **Composite confidence**: 40% correlation + 30% z-significance + 20% liquidity + 10% OU decay. Floor: 0.30
- **Group size cap**: max 15 markets per group, sorted by liquidity
- EV range: 5%–15%. Signals ranked by `confidence × EV`

**engine/mispricing.py** — `MispricingDetector` — structural mispricing detection (guaranteed mathematical edge). Parses market questions to extract (asset, direction, strike, date) and checks monotonicity constraints:
- **Strike monotonicity**: P("BTC reach $90k") ≥ P("BTC reach $100k"). Higher strike = lower probability for "reach"; inverse for "dip"
- **Date monotonicity**: P("BTC reach $100k by March") ≤ P("BTC reach $100k by December"). More time = higher probability
- Violations ≥ 3¢ generate paired signals (YES on underpriced + NO on overpriced)
- EV = (gap/2 − spread/2 − slippage) / entry_price. Range: 2%–25%
- Confidence: 50% violation size + 30% liquidity + 20% volume. Floor: 0.25
- Mispricing signals are prioritized over leader/lagger signals in execution
- Supports: btc, eth, oil, gold, sp500 assets

**engine/ws_client.py** — `PolymarketWS` connects to `wss://ws-subscriptions-clob.polymarket.com/ws/market`. Handles price_change, last_trade_price, and book events. Correctly disambiguates YES/NO tokens and converts all prices to YES-denominated. Tracks spread, bid/ask. Callbacks for price changes and whale trades (≥$500). Auto-reconnects on disconnect.

**utils/db.py** — `Database` wraps asyncpg pool. **Writes only to own tables** (`arb_signals`, `arb_positions`, `arb_stats`). Reads shared quant-engine tables (`positions`, `stats`) via `get_shared_*()` methods (read-only). Position lifecycle: open → monitor (update price/uPnL) → close (TP/SL/timeout). Bankroll tracked independently in `arb_stats`.

**utils/telegram.py** — `TelegramBot` exists but is not currently wired into the main loop.

## Database Tables (owned by this bot)

| Table | Purpose |
|---|---|
| `arb_signals` | Generated signals: market_id, side, ev, group_name, leader info, signal_type (leader_lagger/mispricing) |
| `arb_positions` | Arb trades: side, stake, ev, kelly, tp/sl, status, result, pnl, close_reason |
| `arb_stats` | Singleton (id=1): bankroll, total_pnl, wins, losses |

Shared tables read (not written): `positions`, `stats` from quant-engine.

## Key Design Decisions

- **Full isolation from quant-engine**: writes only to `arb_*` tables, never touches shared `positions`/`stats`
- Position sizing: fixed Kelly fraction (default 5%) of arb bankroll, hard cap $50/trade
- Positions auto-close on take-profit (8%), stop-loss (5%), or timeout (30 min)
- Max 1 signal per tick, max 10 open positions total
- Each market can only have one open position at a time
- Per-market cooldown 5 min, per-group cooldown 10 min after signal
- Warmup period: 30 ticks (~2 min) before detection begins
