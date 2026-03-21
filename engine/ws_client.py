import asyncio
import json
import logging
import time
from typing import Callable, Optional

import websockets

log = logging.getLogger("ws")

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
HEARTBEAT_INTERVAL = 10  # seconds
RECONNECT_DELAY = 5      # seconds


class PolymarketWS:
    """WebSocket client for real-time Polymarket price updates."""

    def __init__(self):
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self._running = False
        self._subscribed_tokens: set = set()
        # token_id -> market_id mapping
        self._token_to_market: dict[str, str] = {}
        # market_id -> latest data
        self.prices: dict[str, dict] = {}
        # callbacks
        self._on_price_change: Optional[Callable] = None
        self._on_trade: Optional[Callable] = None

    def set_callbacks(self, on_price_change=None, on_trade=None):
        """Set callback functions for price changes and trades."""
        self._on_price_change = on_price_change
        self._on_trade = on_trade

    def register_markets(self, markets: list):
        """Register markets and their token IDs for subscription."""
        for m in markets:
            mid = m["id"]
            yes_token = m.get("yes_token")
            no_token = m.get("no_token")
            if yes_token:
                self._token_to_market[yes_token] = mid
                self._subscribed_tokens.add(yes_token)
            if no_token:
                self._token_to_market[no_token] = mid
                self._subscribed_tokens.add(no_token)
            # Initialize price store
            if mid not in self.prices:
                self.prices[mid] = {
                    "yes_price": m.get("yes_price", 0.5),
                    "question": m.get("question", ""),
                    "last_update": time.time(),
                }

    async def connect(self):
        """Connect to WebSocket and start listening."""
        self._running = True
        while self._running:
            try:
                async with websockets.connect(WS_URL, ping_interval=None) as ws:
                    self.ws = ws
                    log.info(f"[WS] Connected, subscribing to {len(self._subscribed_tokens)} tokens")

                    # Subscribe to all tokens
                    await self._subscribe(ws)

                    # Start heartbeat
                    heartbeat_task = asyncio.create_task(self._heartbeat(ws))

                    try:
                        async for message in ws:
                            if message == "PONG":
                                continue
                            try:
                                data = json.loads(message)
                                await self._handle_message(data)
                            except json.JSONDecodeError:
                                continue
                    finally:
                        heartbeat_task.cancel()

            except (websockets.ConnectionClosed, ConnectionError, OSError) as e:
                log.warning(f"[WS] Disconnected: {e}, reconnecting in {RECONNECT_DELAY}s")
                await asyncio.sleep(RECONNECT_DELAY)
            except Exception as e:
                log.error(f"[WS] Error: {e}", exc_info=True)
                await asyncio.sleep(RECONNECT_DELAY)

    async def _subscribe(self, ws):
        """Send subscription message for all tracked tokens."""
        if not self._subscribed_tokens:
            return
        # Subscribe in batches of 100
        tokens = list(self._subscribed_tokens)
        for i in range(0, len(tokens), 100):
            batch = tokens[i:i+100]
            msg = {
                "assets_ids": batch,
                "type": "market",
                "custom_feature_enabled": True,
            }
            await ws.send(json.dumps(msg))
            log.info(f"[WS] Subscribed batch {i//100+1}: {len(batch)} tokens")

    async def add_subscriptions(self, token_ids: list):
        """Dynamically subscribe to new tokens without reconnecting."""
        new_tokens = [t for t in token_ids if t not in self._subscribed_tokens]
        if not new_tokens or not self.ws:
            return
        self._subscribed_tokens.update(new_tokens)
        msg = {
            "assets_ids": new_tokens,
            "operation": "subscribe",
            "custom_feature_enabled": True,
        }
        await self.ws.send(json.dumps(msg))
        log.info(f"[WS] Added {len(new_tokens)} new subscriptions")

    async def _heartbeat(self, ws):
        """Send PING every 10 seconds to keep connection alive."""
        while True:
            try:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                await ws.send("PING")
            except Exception:
                break

    async def _handle_message(self, data: dict):
        """Process incoming WebSocket messages."""
        event_type = data.get("event_type")

        if event_type == "price_change":
            await self._handle_price_change(data)

        elif event_type == "last_trade_price":
            await self._handle_trade(data)

        elif event_type == "book":
            self._handle_book(data)

    async def _handle_price_change(self, data: dict):
        """Handle price_change event — update best bid/ask."""
        for change in data.get("price_changes", []):
            asset_id = change.get("asset_id")
            market_id = self._token_to_market.get(asset_id)
            if not market_id:
                continue

            best_bid = change.get("best_bid")
            best_ask = change.get("best_ask")
            if best_bid and best_ask:
                mid_price = (float(best_bid) + float(best_ask)) / 2
                # Determine if this is YES or NO token
                is_yes = any(
                    m.get("yes_token") == asset_id
                    for m in [self.prices.get(market_id, {})]
                    if "yes_token" in m
                )
                if market_id in self.prices:
                    old_price = self.prices[market_id].get("yes_price", 0)
                    self.prices[market_id]["yes_price"] = round(mid_price, 4)
                    self.prices[market_id]["best_bid"] = float(best_bid)
                    self.prices[market_id]["best_ask"] = float(best_ask)
                    self.prices[market_id]["spread"] = round(float(best_ask) - float(best_bid), 4)
                    self.prices[market_id]["last_update"] = time.time()

                    if self._on_price_change:
                        await self._on_price_change(market_id, old_price, mid_price)

    async def _handle_trade(self, data: dict):
        """Handle last_trade_price event — a trade was executed."""
        asset_id = data.get("asset_id")
        market_id = self._token_to_market.get(asset_id)
        if not market_id:
            return

        price = float(data.get("price", 0))
        size = float(data.get("size", 0))
        side = data.get("side", "")

        if market_id in self.prices:
            self.prices[market_id]["yes_price"] = round(price, 4)
            self.prices[market_id]["last_trade_size"] = size
            self.prices[market_id]["last_trade_side"] = side
            self.prices[market_id]["last_update"] = time.time()

        if self._on_trade and size > 0:
            await self._on_trade(market_id, price, size, side)

    def _handle_book(self, data: dict):
        """Handle book event — full orderbook snapshot."""
        asset_id = data.get("asset_id")
        market_id = self._token_to_market.get(asset_id)
        if not market_id:
            return
        bids = data.get("bids", [])
        asks = data.get("asks", [])
        if bids and asks:
            best_bid = float(bids[0]["price"])
            best_ask = float(asks[0]["price"])
            mid = (best_bid + best_ask) / 2
            if market_id in self.prices:
                self.prices[market_id]["yes_price"] = round(mid, 4)
                self.prices[market_id]["best_bid"] = best_bid
                self.prices[market_id]["best_ask"] = best_ask
                self.prices[market_id]["spread"] = round(best_ask - best_bid, 4)
                self.prices[market_id]["last_update"] = time.time()

    def get_price(self, market_id: str) -> float:
        """Get latest price for a market."""
        return self.prices.get(market_id, {}).get("yes_price", 0)

    def stop(self):
        """Stop the WebSocket client."""
        self._running = False
