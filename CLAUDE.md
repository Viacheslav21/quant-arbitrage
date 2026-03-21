# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Polymarket arbitrage bot that detects leader/lagger price divergences across correlated prediction markets and generates trading signals. Runs as a long-lived async Python process with PostgreSQL persistence and Telegram alerts.

## Running

```bash
pip install -r requirements.txt
python main.py
```

Deployed via Procfile (`web: python main.py`). No test suite or linter is configured.

## Required Environment Variables

Set in `.env` (loaded via python-dotenv):
- `DATABASE_URL` — PostgreSQL connection string (shared with a broader quant-engine system)
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` — for alert delivery
- `SIMULATION` — "true"/"false", controls live vs simulated execution
- `BANKROLL` — starting capital (default 1000)
- Tuning: `ARB_SCAN_INTERVAL`, `ARB_FULL_SCAN_EVERY`, `ARB_TP_PCT`, `ARB_SL_PCT`, `ARB_TIMEOUT_MIN`, `MIN_VOLUME`, `ARB_MAX_OPEN`, `ARB_KELLY_FRAC`, `ARB_CONFIG_TAG`

## Architecture

**main.py** — Event loop: scan → group → detect → execute → monitor positions. Runs on a configurable tick interval (default 4s). Full market fetch happens every N ticks to reduce API load; intermediate ticks reuse cached markets.

**engine/scanner.py** — `PolymarketScanner` fetches active markets from the Gamma API (`gamma-api.polymarket.com`), filtering by volume and liquidity. Paginates up to 500 markets.

**engine/groups.py** — `assign()` bins markets into correlation groups (oil, btc, eth, trump, fed, etc.) via keyword matching on the market question. A market belongs to at most one group; groups with <2 markets are discarded.

**engine/detector.py** — `Detector` maintains a per-market price history (deque). On each tick, it computes recent price moves over a lookback window and identifies a "leader" (largest mover above threshold) and "laggers" (correlated markets that haven't moved yet). Signals are generated for laggers expected to follow the leader, with EV calculated as a fraction of the leader's move. Per-market cooldown prevents repeated signals.

**utils/db.py** — `Database` wraps asyncpg pool. Connects to a shared PostgreSQL instance with pre-existing `positions` and `stats` tables. Creates its own `arb_signals` table on init. Position lifecycle: open → monitor (update price/uPnL) → close (TP/SL/timeout).

**utils/telegram.py** — `TelegramBot` sends HTML-formatted alerts via the Telegram Bot API.

## Key Design Decisions

- The `positions` and `stats` tables are shared with other bots in the quant-engine system; `arb_signals` is owned by this bot
- Position sizing uses a fixed Kelly fraction of bankroll (default 5%)
- Positions auto-close on take-profit (8%), stop-loss (5%), or timeout (30 min)
- Max 3 signals executed per tick, max 10 open positions total
- Each market can only have one open position at a time
