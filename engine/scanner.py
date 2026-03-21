import logging
import json as _json
import httpx

log = logging.getLogger("scanner")

GAMMA_API = "https://gamma-api.polymarket.com"


def _parse_token_ids(m: dict) -> tuple:
    """Extract YES and NO token IDs from market data."""
    token_ids = m.get("clobTokenIds") or []
    if isinstance(token_ids, str):
        token_ids = _json.loads(token_ids)
    yes_token = token_ids[0] if len(token_ids) > 0 else None
    no_token = token_ids[1] if len(token_ids) > 1 else None
    return yes_token, no_token


class PolymarketScanner:
    def __init__(self, config: dict):
        self.config = config
        self.client = httpx.AsyncClient(timeout=15.0)

    async def fetch(self) -> list:
        """Fetch active markets from Polymarket Gamma API with token IDs."""
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
                        raw_prices = _json.loads(raw_prices)
                    yes_price = float(raw_prices[0])
                    if yes_price > 0.97 or yes_price < 0.03:
                        continue
                    yes_token, no_token = _parse_token_ids(m)
                    markets.append({
                        "id":         m["id"],
                        "question":   m.get("question", ""),
                        "yes_price":  round(yes_price, 4),
                        "volume":     vol,
                        "volume_24h": float(m.get("volume24hr") or 0),
                        "liquidity":  liq,
                        "spread":     float(m.get("spread") or 0),
                        "yes_token":  yes_token,
                        "no_token":   no_token,
                    })
                offset += 100
                if len(batch) < 100:
                    break
            log.info(f"[SCANNER] {len(markets)} markets fetched")
            return markets
        except Exception as e:
            log.error(f"[SCANNER] {e}")
            return []

    async def close(self):
        await self.client.aclose()
