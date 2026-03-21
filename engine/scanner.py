import logging
import httpx

log = logging.getLogger("scanner")

GAMMA_API = "https://gamma-api.polymarket.com"


class PolymarketScanner:
    def __init__(self, config: dict):
        self.config = config
        self.client = httpx.AsyncClient(timeout=15.0)

    async def fetch(self) -> list:
        """Fetch active markets from Polymarket Gamma API."""
        try:
            markets = []
            offset = 0
            while len(markets) < 500:
                r = await self.client.get(f"{GAMMA_API}/markets", params={
                    "active": "true", "closed": "false",
                    "order": "volume24hr", "ascending": "false",
                    "limit": 100, "offset": offset,
                })
                batch = r.json() or []
                if not batch:
                    break
                for m in batch:
                    vol = float(m.get("volume") or 0)
                    liq = float(m.get("liquidity") or 0)
                    if vol < self.config["MIN_VOLUME"] or liq < 5000:
                        continue
                    raw_prices = m.get("outcomePrices") or ["0.5", "0.5"]
                    if isinstance(raw_prices, str):
                        import json as _json
                        raw_prices = _json.loads(raw_prices)
                    yes_price = float(raw_prices[0])
                    if yes_price > 0.97 or yes_price < 0.03:
                        continue
                    markets.append({
                        "id":        m["id"],
                        "question":  m.get("question", ""),
                        "yes_price": round(yes_price, 4),
                        "volume":    vol,
                        "volume_24h": float(m.get("volume24hr") or 0),
                        "liquidity": liq,
                        "spread":    float(m.get("spread") or 0),
                    })
                offset += 100
                if len(batch) < 100:
                    break
            log.info(f"[SCANNER] {len(markets)} markets fetched")
            return markets
        except Exception as e:
            log.error(f"[SCANNER] {e}")
            return []

    async def quick_fetch(self, known_ids: set) -> list:
        """Quick fetch top 200 markets by 24h volume. Update prices for known grouped markets."""
        try:
            markets = []
            r = await self.client.get(f"{GAMMA_API}/markets", params={
                "active": "true", "closed": "false",
                "order": "volume24hr", "ascending": "false",
                "limit": 100, "offset": 0,
            })
            batch = r.json() or []
            for m in batch:
                raw_prices = m.get("outcomePrices") or ["0.5", "0.5"]
                if isinstance(raw_prices, str):
                    import json as _json
                    raw_prices = _json.loads(raw_prices)
                yes_price = float(raw_prices[0])
                if yes_price > 0.97 or yes_price < 0.03:
                    continue
                mid = m["id"]
                if mid in known_ids:
                    markets.append({
                        "id":        mid,
                        "question":  m.get("question", ""),
                        "yes_price": round(yes_price, 4),
                        "volume":    float(m.get("volume") or 0),
                        "volume_24h": float(m.get("volume24hr") or 0),
                        "liquidity": float(m.get("liquidity") or 0),
                        "spread":    float(m.get("spread") or 0),
                    })
            return markets
        except Exception as e:
            log.error(f"[SCANNER] quick_fetch: {e}")
            return []

    async def close(self):
        await self.client.aclose()
