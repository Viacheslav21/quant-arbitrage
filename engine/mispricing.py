import re
import logging
import time

log = logging.getLogger("mispricing")

# ── Thresholds ──
MIN_VIOLATION = 0.03      # 3¢ minimum gap to overcome spreads
MAX_SPREAD = 0.04         # skip illiquid markets
MIN_LIQUIDITY = 5000
MIN_VOLUME = 10000
MIN_PRICE = 0.05
MAX_PRICE = 0.95
MIN_EV = 0.02             # 2% floor (structural edge is more reliable)
MAX_EV = 0.25             # 25% cap
MIN_CONFIDENCE = 0.25
COOLDOWN_SEC = 600        # 10 min per-market

# ── Question parsing ──

ASSET_PATTERNS = [
    # Crypto
    (re.compile(r"(?:bitcoin|btc)", re.I), "btc"),
    (re.compile(r"(?:ethereum|eth)\b", re.I), "eth"),
    (re.compile(r"(?:solana|sol)\b", re.I), "sol"),
    (re.compile(r"(?:dogecoin|doge)\b", re.I), "doge"),
    (re.compile(r"(?:xrp|ripple)\b", re.I), "xrp"),
    # Commodities
    (re.compile(r"(?:crude\s*oil|oil\s*\(?cl\)?|brent|wti)", re.I), "oil"),
    (re.compile(r"(?:gold|xau|\(?gc\)?)", re.I), "gold"),
    (re.compile(r"(?:silver|xag|\(?si\)?)", re.I), "silver"),
    (re.compile(r"(?:natural\s*gas|\(?ng\)?)", re.I), "natgas"),
    # Indices & stocks
    (re.compile(r"(?:s&p\s*500|sp500|spx)", re.I), "sp500"),
    (re.compile(r"(?:nasdaq|qqq)", re.I), "nasdaq"),
    (re.compile(r"(?:dow\s*jones|djia)", re.I), "dow"),
    (re.compile(r"(?:tesla|tsla)\b", re.I), "tesla"),
    (re.compile(r"(?:nvidia|nvda)\b", re.I), "nvidia"),
    (re.compile(r"(?:apple|aapl)\b", re.I), "apple"),
    # Macro
    (re.compile(r"(?:fed\s*(?:funds?\s*)?rate|interest\s*rate)", re.I), "fedrate"),
    (re.compile(r"(?:unemployment\s*(?:rate)?)", re.I), "unemployment"),
    (re.compile(r"(?:inflation|cpi)\b", re.I), "inflation"),
    (re.compile(r"(?:gdp)\b", re.I), "gdp"),
    # Politics
    (re.compile(r"(?:trump\s*approval)", re.I), "trump_approval"),
    (re.compile(r"(?:biden\s*approval)", re.I), "biden_approval"),
    # Forex
    (re.compile(r"(?:eur/?usd)", re.I), "eurusd"),
    (re.compile(r"(?:usd/?jpy)", re.I), "usdjpy"),
    # Other metrics with numbers
    (re.compile(r"(?:twitter|x\.com)\s*(?:followers|posts|tweets)", re.I), "tweets"),
    (re.compile(r"(?:subscribers|views|downloads)", re.I), "metric"),
]

REACH_RE = re.compile(r"(?:reach|hit\s*\(?high\)?|above|exceed|surpass|over|top)", re.I)
DIP_RE = re.compile(r"(?:dip\s*to|hit\s*\(?low\)?|below|drop\s*to|fall\s*to|under)", re.I)
STRIKE_RE = re.compile(r"\$\s*([\d,]+(?:\.\d+)?)\s*([kK])?\b")

MONTH_MAP = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4,
    "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _parse_asset(question: str) -> str | None:
    for pattern, name in ASSET_PATTERNS:
        if pattern.search(question):
            return name
    return None


def _parse_direction(question: str) -> str | None:
    q = question.lower()
    # Check dip first (more specific — "dip to" vs just "reach")
    if DIP_RE.search(q):
        return "dip"
    if REACH_RE.search(q):
        return "reach"
    return None


def _parse_strike(question: str) -> float | None:
    m = STRIKE_RE.search(question)
    if not m:
        return None
    val = float(m.group(1).replace(",", ""))
    suffix = m.group(2)
    if suffix and suffix.lower() == "k":
        val *= 1000
    return val


def _parse_date_key(question: str) -> int:
    """Return numeric date key for ordering. Higher = later. 0 if unparseable."""
    q = question.lower()
    year = 26  # default 2026
    year_m = re.search(r"20(\d{2})", q)
    if year_m:
        year = int(year_m.group(1))

    for month_name, month_num in MONTH_MAP.items():
        if month_name in q:
            # Try to find day
            day_m = re.search(month_name + r"\s+(\d{1,2})", q)
            day = int(day_m.group(1)) if day_m else 28  # default end of month
            return year * 10000 + month_num * 100 + day

    # Just year ("before 2027")
    if year_m:
        return year * 10000 + 1231
    return 0


def _parse_market(question: str) -> dict | None:
    """Parse a market question into structured data. Returns None if unparseable."""
    asset = _parse_asset(question)
    direction = _parse_direction(question)
    strike = _parse_strike(question)
    if not asset or not direction or not strike:
        return None
    return {
        "asset": asset,
        "direction": direction,
        "strike": strike,
        "date_key": _parse_date_key(question),
    }


class MispricingDetector:
    """Detects mathematical mispricings between related markets.

    Monotonicity constraints:
    - "reach" markets: higher strike → lower P(yes)
    - "dip" markets: lower strike → lower P(yes)
    - Same strike, later date → higher P(yes)

    When violated → guaranteed mispricing.
    """

    def __init__(self):
        self._cooldown: dict[str, float] = {}

    def detect(self, markets: list) -> list:
        """Find monotonicity violations across all markets."""
        now = time.time()
        self._cooldown = {k: v for k, v in self._cooldown.items() if now - v < COOLDOWN_SEC}

        # Parse all markets
        parsed = []
        for m in markets:
            if m["yes_price"] < MIN_PRICE or m["yes_price"] > MAX_PRICE:
                continue
            if m.get("spread", 1) > MAX_SPREAD:
                continue
            if m.get("liquidity", 0) < MIN_LIQUIDITY:
                continue
            if m.get("volume", 0) < MIN_VOLUME:
                continue
            info = _parse_market(m["question"])
            if not info:
                continue
            parsed.append({"market": m, **info})

        # Group into constraint families
        # Type A: same (asset, direction, date_bucket) — varying strikes
        strike_families: dict[tuple, list] = {}
        # Type B: same (asset, direction, strike) — varying dates
        date_families: dict[tuple, list] = {}

        for p in parsed:
            date_bucket = p["date_key"] // 100 * 100  # year-month
            if date_bucket > 0:
                key_a = (p["asset"], p["direction"], date_bucket)
                strike_families.setdefault(key_a, []).append(p)

            if p["date_key"] > 0:
                key_b = (p["asset"], p["direction"], p["strike"])
                date_families.setdefault(key_b, []).append(p)

        signals = []

        # Check strike monotonicity
        for key, family in strike_families.items():
            if len(family) < 2:
                continue
            family.sort(key=lambda x: x["strike"])
            signals.extend(self._check_strike(family, now))

        # Check date monotonicity
        for key, family in date_families.items():
            if len(family) < 2:
                continue
            family.sort(key=lambda x: x["date_key"])
            signals.extend(self._check_date(family, now))

        # Deduplicate by market_id (keep highest EV)
        seen: dict[str, dict] = {}
        for s in signals:
            mid = s["market_id"]
            if mid not in seen or s["ev"] > seen[mid]["ev"]:
                seen[mid] = s
        signals = list(seen.values())

        signals.sort(key=lambda s: s.get("confidence", 0) * s["ev"], reverse=True)
        return signals

    def _check_strike(self, family: list, now: float) -> list:
        """Check strike monotonicity within a date bucket."""
        signals = []
        direction = family[0]["direction"]

        for i in range(len(family) - 1):
            lo = family[i]      # lower strike
            hi = family[i + 1]  # higher strike
            lo_m = lo["market"]
            hi_m = hi["market"]
            lo_p = lo_m["yes_price"]
            hi_p = hi_m["yes_price"]

            if direction == "reach":
                # P(reach lower) should be >= P(reach higher)
                # violation: hi_p > lo_p
                if hi_p > lo_p + MIN_VIOLATION:
                    gap = hi_p - lo_p
                    signals.extend(self._make_signals(
                        underpriced=lo_m, overpriced=hi_m, gap=gap,
                        reason=f"{lo['asset']} reach: ${lo['strike']:,.0f} underpriced vs ${hi['strike']:,.0f}",
                        now=now,
                    ))
            elif direction == "dip":
                # P(dip to higher threshold) should be >= P(dip to lower threshold)
                # e.g. P(dip to $65k) >= P(dip to $55k)
                # violation: lo_p > hi_p (lower strike priced higher)
                if lo_p > hi_p + MIN_VIOLATION:
                    gap = lo_p - hi_p
                    signals.extend(self._make_signals(
                        underpriced=hi_m, overpriced=lo_m, gap=gap,
                        reason=f"{lo['asset']} dip: ${hi['strike']:,.0f} underpriced vs ${lo['strike']:,.0f}",
                        now=now,
                    ))
        return signals

    def _check_date(self, family: list, now: float) -> list:
        """Check date monotonicity within a strike bucket."""
        signals = []

        for i in range(len(family) - 1):
            early = family[i]
            later = family[i + 1]
            early_m = early["market"]
            later_m = later["market"]
            early_p = early_m["yes_price"]
            later_p = later_m["yes_price"]

            # Later date should have >= probability (more time)
            # violation: early_p > later_p
            if early_p > later_p + MIN_VIOLATION:
                gap = early_p - later_p
                signals.extend(self._make_signals(
                    underpriced=later_m, overpriced=early_m, gap=gap,
                    reason=f"{early['asset']} ${early['strike']:,.0f}: later date underpriced",
                    now=now,
                ))
        return signals

    def _make_signals(self, underpriced: dict, overpriced: dict,
                      gap: float, reason: str, now: float) -> list:
        """Generate signal pair for a mispricing."""
        signals = []
        min_liq = min(underpriced.get("liquidity", 0), overpriced.get("liquidity", 0))
        min_vol = min(underpriced.get("volume", 0), overpriced.get("volume", 0))

        confidence = (
            min(gap / 0.10, 1.0) * 0.50
            + min(min_liq / 30_000, 1.0) * 0.30
            + min(min_vol / 100_000, 1.0) * 0.20
        )
        if confidence < MIN_CONFIDENCE:
            return []

        # Buy YES on underpriced
        mid = underpriced["id"]
        if mid not in self._cooldown:
            price = underpriced["yes_price"]
            spread = underpriced.get("spread", 0)
            ev = (gap / 2 - spread / 2 - 0.005) / price if price > 0 else 0
            if MIN_EV <= ev <= MAX_EV:
                signals.append(self._signal(
                    underpriced, "YES", price, ev, gap, confidence, reason, spread))

        # Buy NO on overpriced
        mid = overpriced["id"]
        if mid not in self._cooldown:
            price = 1 - overpriced["yes_price"]
            spread = overpriced.get("spread", 0)
            ev = (gap / 2 - spread / 2 - 0.005) / price if price > 0 else 0
            if MIN_EV <= ev <= MAX_EV:
                signals.append(self._signal(
                    overpriced, "NO", price, ev, gap, confidence, reason, spread))

        return signals

    def _signal(self, market, side, side_price, ev, gap, confidence, reason, spread):
        return {
            "market_id":   market["id"],
            "question":    market["question"],
            "side":        side,
            "side_price":  round(side_price, 4),
            "yes_price":   market["yes_price"],
            "ev":          round(ev, 4),
            "group":       "mispricing",
            "leader_q":    reason[:60],
            "leader_move": round(gap, 4),
            "spread":      spread,
            "confidence":  round(confidence, 3),
            "signal_type": "mispricing",
        }

    def mark_cooldown(self, market_id: str):
        self._cooldown[market_id] = time.time()
