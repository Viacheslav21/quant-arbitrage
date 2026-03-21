import logging
import time
from collections import deque

log = logging.getLogger("detector")

# Detection parameters
LEADER_MIN_MOVE = 0.03   # 3¢ minimum move to be a "leader"
LAGGER_MAX_MOVE = 0.01   # 1¢ max move to be considered "lagging"
LOOKBACK_TICKS = 6       # ~24 seconds at 4s interval
MIN_SPREAD = 0.05        # skip markets with spread > 5¢
MIN_PRICE = 0.10         # skip markets priced < 10¢ (bad risk/reward)
MAX_PRICE = 0.90         # skip markets priced > 90¢
COOLDOWN = 300            # 5 min cooldown per market
GROUP_COOLDOWN = 600      # 10 min cooldown per group after signal
MAX_HISTORY = 60          # ~4 min of ticks at 4s interval
WARMUP_TICKS = 30         # ~2 min warmup before detecting


class Detector:
    def __init__(self):
        self.history: dict[str, deque] = {}  # market_id -> deque of (timestamp, yes_price)
        self._cooldown: dict[str, float] = {}  # market_id -> last signal timestamp
        self._group_cooldown: dict[str, float] = {}  # group_name -> last signal timestamp
        self._tick_count: int = 0

    def update(self, markets: list):
        """Feed new prices for markets."""
        now = time.time()
        self._tick_count += 1
        for m in markets:
            mid = m["id"]
            if mid not in self.history:
                self.history[mid] = deque(maxlen=MAX_HISTORY)
            self.history[mid].append((now, m["yes_price"]))

    def detect(self, group_name: str, markets: list) -> list:
        """Detect leader/lagger divergences within a group.
        Returns list of signal dicts."""
        now = time.time()

        # Warmup — need enough ticks to establish baseline
        if self._tick_count < WARMUP_TICKS:
            return []

        # Group cooldown — don't spam signals from same group
        if group_name in self._group_cooldown and now - self._group_cooldown[group_name] < GROUP_COOLDOWN:
            return []

        # Clean expired cooldowns
        self._cooldown = {k: v for k, v in self._cooldown.items() if now - v < COOLDOWN}

        # Calculate recent moves for each market in the group
        moves = {}
        for m in markets:
            mid = m["id"]
            hist = self.history.get(mid)
            if not hist or len(hist) < LOOKBACK_TICKS:
                continue
            old_price = hist[-LOOKBACK_TICKS][1]
            new_price = m["yes_price"]
            moves[mid] = {
                "market": m,
                "move": new_price - old_price,
                "abs_move": abs(new_price - old_price),
            }

        if len(moves) < 2:
            return []

        # Find leader: largest absolute move above threshold
        leader = None
        leader_mid = None
        for mid, data in moves.items():
            if data["abs_move"] >= LEADER_MIN_MOVE:
                if leader is None or data["abs_move"] > leader["abs_move"]:
                    leader = data
                    leader_mid = mid

        if not leader:
            return []

        # Find laggers: markets that haven't moved
        signals = []
        for mid, data in moves.items():
            if mid == leader_mid:
                continue
            if mid in self._cooldown:
                continue
            if data["abs_move"] > LAGGER_MAX_MOVE:
                continue  # already moving, not a lagger
            m = data["market"]
            if m.get("spread", 0) > MIN_SPREAD:
                continue  # too illiquid
            # Skip extreme prices — bad risk/reward
            if m["yes_price"] < MIN_PRICE or m["yes_price"] > MAX_PRICE:
                continue

            # Direction: account for inverse correlation
            # leader direction × lagger direction = expected move direction
            leader_dir = leader["market"].get("direction", 1)
            lagger_dir = m.get("direction", 1)
            # same×same=1 (follow), same×inverse=-1 (opposite), inverse×inverse=1 (follow)
            correlation = leader_dir * lagger_dir
            leader_up = leader["move"] > 0

            if (leader_up and correlation > 0) or (not leader_up and correlation < 0):
                side = "YES"
                side_price = m["yes_price"]
            else:
                side = "NO"
                side_price = 1 - m["yes_price"]

            # Expected move = fraction of leader's move
            expected_move = leader["abs_move"] * 0.5
            ev = expected_move / side_price if side_price > 0 else 0

            if ev < 0.03:  # skip tiny edge
                continue
            ev = min(ev, 0.50)  # cap EV at 50% — anything higher is likely noise

            signals.append({
                "market_id":  mid,
                "question":   m["question"],
                "side":       side,
                "side_price": round(side_price, 4),
                "yes_price":  m["yes_price"],
                "ev":         round(ev, 4),
                "group":      group_name,
                "leader_q":   leader["market"]["question"][:60],
                "leader_move": round(leader["move"], 4),
                "spread":     m.get("spread", 0),
            })

        return signals

    def mark_cooldown(self, market_id: str, group_name: str = None):
        """Mark market and group as recently signaled."""
        self._cooldown[market_id] = time.time()
        if group_name:
            self._group_cooldown[group_name] = time.time()
