import logging
import time
import math
from collections import deque

log = logging.getLogger("detector")

# ── Timing ──
LOOKBACK_TICKS = 8        # ~32s at 4s interval for move window
MIN_HISTORY = 60          # minimum ticks before computing stats (~4 min, was 100)
MAX_HISTORY = 500         # ~33 min rolling window
WARMUP_TICKS = 30         # ~2 min warmup before detection
COOLDOWN = 300            # 5 min per-market cooldown
GROUP_COOLDOWN = 600      # 10 min per-group cooldown
MAX_GROUP_SIZE = 15       # cap markets per group (top by liquidity)

# ── Statistical thresholds ──
LEADER_Z_THRESHOLD = 1.5  # leader must move ≥ 1.5σ (relaxed from 2.0 — prediction markets move slowly)
LAGGER_Z_THRESHOLD = 0.7  # lagger must be relatively quiet (relaxed from 0.5)
MIN_CORRELATION = 0.25    # minimum Pearson ρ (relaxed from 0.35 — prediction markets are weakly correlated)
MIN_VOLATILITY = 0.0005   # skip markets with near-zero vol (relaxed from 0.001)

# ── Price / spread filters ──
MIN_PRICE = 0.10
MAX_PRICE = 0.90
MAX_SPREAD = 0.05         # 5¢

# ── EV / confidence gates ──
MIN_EV = 0.05             # 5% minimum edge after costs
MAX_EV = 0.15             # 15% cap — beyond this is model error
MIN_CONFIDENCE = 0.30     # composite confidence floor


class MarketStats:
    """Rolling price statistics for a single market."""
    __slots__ = ("prices", "returns")

    def __init__(self):
        self.prices: deque = deque(maxlen=MAX_HISTORY)    # (timestamp, price)
        self.returns: deque = deque(maxlen=MAX_HISTORY)   # tick-to-tick Δprice

    def update(self, timestamp: float, price: float):
        if self.prices:
            self.returns.append(price - self.prices[-1][1])
        self.prices.append((timestamp, price))

    @property
    def ready(self) -> bool:
        return len(self.returns) >= MIN_HISTORY

    @property
    def volatility(self) -> float:
        """Sample standard deviation of returns."""
        n = len(self.returns)
        if n < 2:
            return 0.0
        mean = sum(self.returns) / n
        var = sum((r - mean) ** 2 for r in self.returns) / (n - 1)
        return math.sqrt(var)

    def recent_move(self, lookback: int = LOOKBACK_TICKS) -> float:
        """Cumulative price change over last *lookback* ticks."""
        if len(self.prices) < lookback:
            return 0.0
        return self.prices[-1][1] - self.prices[-lookback][1]

    def z_score(self, lookback: int = LOOKBACK_TICKS) -> float:
        """Z-score of recent move: move / σ."""
        vol = self.volatility
        if vol < MIN_VOLATILITY:
            return 0.0
        return self.recent_move(lookback) / vol


class Detector:
    def __init__(self):
        self.stats: dict[str, MarketStats] = {}
        self._cooldown: dict[str, float] = {}
        self._group_cooldown: dict[str, float] = {}
        self._tick_count: int = 0

    # ── Public API ──

    def update(self, markets: list):
        """Feed latest prices into per-market rolling stats."""
        now = time.time()
        for m in markets:
            mid = m["id"]
            if mid not in self.stats:
                self.stats[mid] = MarketStats()
            self.stats[mid].update(now, m["yes_price"])

    def detect(self, group_name: str, markets: list) -> list:
        """Detect leader/lagger divergences using z-scores, correlation, and OU mean-reversion."""
        now = time.time()

        if self._tick_count < WARMUP_TICKS:
            log.debug(f"[DETECT] {group_name}: warming up ({self._tick_count}/{WARMUP_TICKS})")
            return []
        if group_name in self._group_cooldown and now - self._group_cooldown[group_name] < GROUP_COOLDOWN:
            remaining = GROUP_COOLDOWN - (now - self._group_cooldown[group_name])
            log.debug(f"[DETECT] {group_name}: group cooldown ({remaining:.0f}s remaining)")
            return []

        # Expire old cooldowns
        self._cooldown = {k: v for k, v in self._cooldown.items() if now - v < COOLDOWN}

        # Cap group size — keep most liquid
        if len(markets) > MAX_GROUP_SIZE:
            markets = sorted(markets, key=lambda m: m.get("liquidity", 0), reverse=True)[:MAX_GROUP_SIZE]

        # ── 1. Z-scores for every market in the group ──
        scored: dict[str, dict] = {}
        for m in markets:
            mid = m["id"]
            st = self.stats.get(mid)
            if not st or not st.ready:
                continue
            z = st.z_score()
            scored[mid] = {
                "market": m,
                "z": z,
                "abs_z": abs(z),
                "move": st.recent_move(),
                "vol": st.volatility,
            }

        not_ready = len(markets) - len(scored)
        if not_ready > 0:
            log.debug(f"[DETECT] {group_name}: {len(scored)}/{len(markets)} markets ready ({not_ready} still collecting)")
        if len(scored) < 2:
            log.debug(f"[DETECT] {group_name}: need ≥2 scored markets, have {len(scored)}")
            return []

        # ── 2. Find leader (largest |z| ≥ threshold) ──
        leader = None
        leader_mid = None
        for mid, data in scored.items():
            if data["abs_z"] >= LEADER_Z_THRESHOLD:
                if leader is None or data["abs_z"] > leader["abs_z"]:
                    leader = data
                    leader_mid = mid

        if not leader:
            log.debug(f"[DETECT] {group_name}: no leader found (max |z|={max((d['abs_z'] for d in scored.values()), default=0):.2f}, need {LEADER_Z_THRESHOLD})")
            return []

        log.debug(f"[DETECT] {group_name}: leader '{leader['market']['question'][:40]}' z={leader['z']:.2f}")

        # ── 3. Evaluate each lagger candidate ──
        signals = []
        candidates = len([mid for mid in scored if mid != leader_mid and mid not in self._cooldown])
        log.debug(f"[DETECT] {group_name}: evaluating {candidates} lagger candidates")
        for mid, data in scored.items():
            if mid == leader_mid:
                continue
            if mid in self._cooldown:
                log.debug(f"[DETECT] skip '{data['market']['question'][:35]}': cooldown")
                continue
            q_short = data["market"]["question"][:35]
            if data["abs_z"] > LAGGER_Z_THRESHOLD:
                log.debug(f"[DETECT] skip '{q_short}': |z|={data['abs_z']:.2f} > {LAGGER_Z_THRESHOLD} (already moving)")
                continue

            m = data["market"]
            if m["yes_price"] < MIN_PRICE or m["yes_price"] > MAX_PRICE:
                log.debug(f"[DETECT] skip '{q_short}': price {m['yes_price']:.2f} out of range")
                continue
            if m.get("spread", 0) > MAX_SPREAD:
                log.debug(f"[DETECT] skip '{q_short}': spread {m.get('spread',0):.3f} > {MAX_SPREAD}")
                continue

            # ── Correlation ──
            raw_corr = self._pearson(leader_mid, mid)
            leader_dir = leader["market"].get("direction", 1)
            lagger_dir = m.get("direction", 1)
            eff_corr = raw_corr * leader_dir * lagger_dir

            if abs(eff_corr) < MIN_CORRELATION:
                log.debug(f"[DETECT] skip '{q_short}': |ρ|={abs(eff_corr):.3f} < {MIN_CORRELATION}")
                continue

            # ── OU half-life — skip if too slow to converge ──
            hl_ticks = self._ou_half_life(leader_mid, mid)
            timeout_ticks = 30 * 60 / 4  # 450 ticks = 30 min
            if hl_ticks > timeout_ticks:
                log.debug(f"[DETECT] skip '{q_short}': OU HL={hl_ticks:.0f} ticks > timeout {timeout_ticks:.0f}")
                continue

            # ── Expected move (OU-informed) ──
            # Hold for min(timeout, 3 × half-life) — captures ~87.5 % of convergence
            hold_ticks = min(timeout_ticks, hl_ticks * 3)
            decay = 1 - math.exp(-math.log(2) * hold_ticks / hl_ticks)
            expected_move = abs(leader["move"]) * abs(eff_corr) * decay

            # Subtract execution costs: half-spread + estimated slippage (0.5¢)
            spread_cost = m.get("spread", 0) / 2
            slippage = 0.005
            net_move = expected_move - spread_cost - slippage
            if net_move <= 0:
                log.debug(f"[DETECT] skip '{q_short}': net_move={net_move:.4f} ≤ 0 (exp={expected_move:.4f} cost={spread_cost+slippage:.4f})")
                continue

            # ── Side ──
            leader_up = leader["move"] > 0
            if (leader_up and eff_corr > 0) or (not leader_up and eff_corr < 0):
                side, side_price = "YES", m["yes_price"]
            else:
                side, side_price = "NO", 1 - m["yes_price"]
            if side_price <= 0:
                continue

            ev = net_move / side_price
            if ev < MIN_EV:
                log.debug(f"[DETECT] skip '{q_short}': EV={ev*100:.1f}% < {MIN_EV*100:.0f}%")
                continue
            if ev > MAX_EV:
                log.debug(f"[DETECT] skip '{q_short}': EV={ev*100:.1f}% > cap {MAX_EV*100:.0f}%")
                continue

            # ── Composite confidence ──
            # 40 % correlation + 30 % leader z-significance + 20 % liquidity + 10 % OU decay
            conf = (
                min(abs(eff_corr), 1.0) * 0.40
                + min(leader["abs_z"] / 3.0, 1.0) * 0.30
                + min(m.get("liquidity", 0) / 50_000, 1.0) * 0.20
                + decay * 0.10
            )
            if conf < MIN_CONFIDENCE:
                log.debug(f"[DETECT] skip '{q_short}': conf={conf:.2f} < {MIN_CONFIDENCE}")
                continue

            log.info(f"[DETECT] ✓ SIGNAL '{q_short}' {side} | ρ={eff_corr:.3f} z={leader['z']:.2f} EV={ev*100:.1f}% conf={conf:.2f} HL={hl_ticks*4/60:.1f}m")
            signals.append({
                "market_id":   mid,
                "question":    m["question"],
                "side":        side,
                "side_price":  round(side_price, 4),
                "yes_price":   m["yes_price"],
                "ev":          round(ev, 4),
                "group":       group_name,
                "leader_q":    leader["market"]["question"][:60],
                "leader_move": round(leader["move"], 4),
                "leader_z":    round(leader["z"], 2),
                "correlation": round(eff_corr, 3),
                "half_life_m": round(hl_ticks * 4 / 60, 1),  # minutes
                "confidence":  round(conf, 3),
                "spread":      m.get("spread", 0),
            })

        # Rank by risk-adjusted edge: confidence × EV
        signals.sort(key=lambda s: s["confidence"] * s["ev"], reverse=True)
        if signals:
            log.info(f"[DETECT] {group_name}: {len(signals)} leader/lagger signal(s) found")
        return signals

    def mark_cooldown(self, market_id: str, group_name: str = None):
        self._cooldown[market_id] = time.time()
        if group_name:
            self._group_cooldown[group_name] = time.time()

    # ── Private helpers ──

    def _pearson(self, mid_a: str, mid_b: str) -> float:
        """Rolling Pearson correlation between two markets' return series."""
        sa = self.stats.get(mid_a)
        sb = self.stats.get(mid_b)
        if not sa or not sb or not sa.ready or not sb.ready:
            return 0.0

        n = min(len(sa.returns), len(sb.returns))
        if n < 10:
            return 0.0

        ra = list(sa.returns)[-n:]
        rb = list(sb.returns)[-n:]

        mean_a = sum(ra) / n
        mean_b = sum(rb) / n

        cov = sum((a - mean_a) * (b - mean_b) for a, b in zip(ra, rb)) / (n - 1)
        var_a = sum((a - mean_a) ** 2 for a in ra) / (n - 1)
        var_b = sum((b - mean_b) ** 2 for b in rb) / (n - 1)

        if var_a <= 0 or var_b <= 0:
            return 0.0
        return cov / math.sqrt(var_a * var_b)

    def _ou_half_life(self, mid_a: str, mid_b: str) -> float:
        """Ornstein-Uhlenbeck half-life of the pair spread.

        Regresses ΔS_t = α + β·S_{t-1}.  For a mean-reverting spread β < 0
        and half_life = −ln(2) / ln(1 + β).  Returns ticks (× 4 for seconds).
        """
        sa = self.stats.get(mid_a)
        sb = self.stats.get(mid_b)
        if not sa or not sb:
            return float("inf")

        n = min(len(sa.prices), len(sb.prices))
        if n < 50:
            return float("inf")

        pa = [sa.prices[len(sa.prices) - n + i][1] for i in range(n)]
        pb = [sb.prices[len(sb.prices) - n + i][1] for i in range(n)]
        spreads = [a - b for a, b in zip(pa, pb)]

        # OLS:  ΔS = α + β * S_{t-1}
        delta = [spreads[i] - spreads[i - 1] for i in range(1, len(spreads))]
        lag = spreads[:-1]
        k = len(delta)
        if k < 5:
            return float("inf")

        mx = sum(lag) / k
        my = sum(delta) / k
        ss_xx = sum((x - mx) ** 2 for x in lag)
        ss_xy = sum((x - mx) * (y - my) for x, y in zip(lag, delta))

        if ss_xx == 0:
            return float("inf")

        beta = ss_xy / ss_xx
        if beta >= 0:
            return float("inf")  # not mean-reverting

        try:
            hl = -math.log(2) / math.log(1 + beta)
        except (ValueError, ZeroDivisionError):
            return float("inf")

        return max(hl, 1.0)
