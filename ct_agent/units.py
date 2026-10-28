"""The quote asset and amount conversions. VIGIL trades against SOL; tokens carry their own decimals.

Positions, limits and P&L are kept in whole SOL (floats). Chain amounts are integers in base units:
lamports for SOL (9 decimals) and the mint's own decimals for a token (pump.fun coins use 6).
"""
from __future__ import annotations

QUOTE_SYMBOL = "SOL"
SOL_DECIMALS = 9
LAMPORTS = 10 ** SOL_DECIMALS


def to_base(amount: float, decimals: int) -> int:
    """Whole units -> integer base units, rounded down so we never spend more than we have."""
    return int(amount * 10 ** decimals)


def from_base(amount: int | str, decimals: int) -> float:
    return int(amount) / 10 ** decimals


def sol(lamports: int | str) -> float:
    return from_base(lamports, SOL_DECIMALS)


def lamports(sol_amount: float) -> int:
    return to_base(sol_amount, SOL_DECIMALS)
