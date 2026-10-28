"""Quote-asset amounts with their unit attached.

Amounts are in the quote asset (SOL, see units.py) or a coin's own units.
This type keeps the unit next to the number so nothing downstream can
print raw quote units as dollars.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class QuoteAmount:
    raw: int
    decimals: int
    symbol: str

    @property
    def value(self) -> float:
        return self.raw / (10 ** self.decimals)

    def usd(self, unit_price_usd: float | None) -> float | None:
        return None if unit_price_usd is None else self.value * unit_price_usd

    def label(self, unit_price_usd: float | None = None) -> str:
        base = f"{self.value:.4g} {self.symbol}"
        usd = self.usd(unit_price_usd)
        return f"{base} (~${usd:,.0f} indicative)" if usd is not None else base
