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


def assign(markets: list) -> dict:
    """Assign markets to correlation groups by keyword matching.
    Returns {group_name: [market, ...]} with only groups having 2+ markets."""
    groups = {}
    for m in markets:
        q = m["question"].lower()
        for group_name, keywords in CORRELATION_GROUPS.items():
            if any(kw in q for kw in keywords):
                groups.setdefault(group_name, []).append(m)
                break  # one group per market to avoid double-counting

    # Only keep groups with 2+ markets (need at least leader + lagger)
    result = {k: v for k, v in groups.items() if len(v) >= 2}
    return result
