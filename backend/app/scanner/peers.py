"""Peer symbols for the peer strategies (SMT divergence, pairs, cointegration, statistical arbitrage) — Phase 8.

Peers are **configured per symbol by the user**. This module only *suggests* candidates from a short list of
instrument groups that commonly move together (same direction), restricted to what the connected broker actually
offers. Suggestions are never applied without an explicit request, and they are labelled as suggestions: whether
two instruments are correlated enough on a given broker and period is something the user should check.

Inverse relationships (e.g. EURUSD vs USDCHF) are deliberately not suggested: the original strategies treat their
first peer as positively correlated unless a parameter says otherwise.
"""
from __future__ import annotations

# Each group lists canonical names (and common broker aliases for indices/energies) of instruments that usually
# move in the same direction.
GROUPS: list[list[str]] = [
    ["EURUSD", "GBPUSD"],
    ["AUDUSD", "NZDUSD"],
    ["EURJPY", "GBPJPY"],
    ["AUDJPY", "NZDJPY"],
    ["XAUUSD", "XAGUSD"],
    ["US30", "DJ30", "US500", "SPX500", "NAS100", "USTEC", "US100"],
    ["DE40", "GER40", "DAX40", "EU50", "STOXX50"],
    ["UKOIL", "BRENT", "USOIL", "WTI"],
    ["BTCUSD", "ETHUSD"],
]
MAX_PEERS = 3


def suggest(canonical: str, offered: dict[str, str]) -> list[str]:
    """Suggested peer broker symbols for one symbol. `offered` maps canonical -> broker symbol for the symbols the
    same broker connection currently offers."""
    key = (canonical or "").upper()
    for group in GROUPS:
        if key in group:
            return [offered[c] for c in group if c != key and c in offered][:MAX_PEERS]
    return []
