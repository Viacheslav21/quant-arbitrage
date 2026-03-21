import logging

log = logging.getLogger("groups")

# Keyword-based correlation groups
# Markets matching the same group are expected to move together
CORRELATION_GROUPS = {
    "oil":      ["oil price", "oil above", "oil below", "crude", "brent", "wti", "opec"],
    "btc":      ["bitcoin price", "bitcoin above", "bitcoin below", "bitcoin reach",
                 "bitcoin dip", "btc above", "btc below"],
    "eth":      ["ethereum price", "ethereum above", "ethereum below", "eth above", "eth below"],
    "trump":    ["trump", "tariff", "executive order"],
    "iran":     ["iran", "iranian", "tehran", "fordow", "kharg"],
    "ukraine":  ["ukraine", "ceasefire", "zelensky", "russia ukraine"],
    "israel":   ["israel", "gaza", "hamas", "hezbollah", "netanyahu"],
    "fed":      ["fed rate", "rate cut", "rate hike", "powell", "fomc", "federal reserve"],
    "gold":     ["gold price", "gold above", "gold below", "xau"],
    "sp500":    ["s&p 500", "s&p500", "sp500", "stock market crash"],
}

# Inverse keywords: if question contains these, it moves OPPOSITE to the group
# e.g., "Bitcoin above $100k" = bullish (same), "Bitcoin dip to $60k" = bearish (inverse)
INVERSE_KEYWORDS = {
    "btc":  ["dip", "below", "crash", "drop", "fall"],
    "eth":  ["dip", "below", "crash", "drop", "fall"],
    "oil":  ["below", "crash", "drop", "fall"],
    "gold": ["below", "crash", "drop", "fall"],
    "sp500": ["drop", "crash", "fall", "below"],
}


def _is_inverse(group_name: str, question: str) -> bool:
    """Check if market moves inverse to the group direction."""
    inv_kws = INVERSE_KEYWORDS.get(group_name, [])
    q = question.lower()
    return any(kw in q for kw in inv_kws)


def assign(markets: list) -> dict:
    """Assign markets to correlation groups by keyword matching.
    Each market gets a 'direction' field: 1 (same) or -1 (inverse).
    Returns {group_name: [market, ...]} with only groups having 2+ markets."""
    groups = {}
    for m in markets:
        q = m["question"].lower()
        for group_name, keywords in CORRELATION_GROUPS.items():
            if any(kw in q for kw in keywords):
                m_copy = dict(m)
                m_copy["direction"] = -1 if _is_inverse(group_name, q) else 1
                groups.setdefault(group_name, []).append(m_copy)
                break  # one group per market to avoid double-counting

    # Only keep groups with 2+ markets (need at least leader + lagger)
    result = {k: v for k, v in groups.items() if len(v) >= 2}
    return result
