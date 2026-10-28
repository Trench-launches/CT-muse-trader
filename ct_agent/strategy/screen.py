"""Screen a Solana coin: prototype filters, not a profitable strategy.

Blockers (the coin fails the screen):
  * it isn't priced against SOL (risk limits and sizes are in SOL);
  * mint safety: its creator can still mint more or freeze holders' coins, or a Token-2022 extension can take,
    block or tax holders' coins;
  * no market data, or 1h volume / liquidity under the minimums;
  * holders: the creator, or the 10 biggest wallets (not counting bonding curves, pools or burn addresses), hold
    too much of the supply.
Flags (worth knowing, not disqualifying): volume many times the liquidity (wash trading), one wallet holding a
big share.

The scanner's buy rule doesn't wait on the screen (only the wallet's limits can stop a triggered buy, and unsafe
mints are never tradeable at all); the screen's findings go to Grok with every analysis.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..config import Settings
from ..units import QUOTE_SYMBOL


@dataclass
class Candidate:
    token: str
    stage: str | None            # 'curve' (launchpad bonding curve) | 'pool' (graduated) | None (unknown)
    score: float
    flags: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def eligible(self) -> bool:
        return not self.blockers


def screen_token(s: Settings, *, token: str, stage: str | None, pool_found: bool, volume_h1_usd: float | None,
                 liquidity_usd: float | None, volume_h24_usd: float | None, safety: list[str] | None = None,
                 holders: dict[str, Any] | None = None, quote_symbol: str = QUOTE_SYMBOL) -> Candidate:
    c = Candidate(token=token, stage=stage, score=0.0)
    _quote_check(c, quote_symbol)
    for why in safety or []:
        c.blockers.append(f"unsafe mint: {why}")
    if not pool_found:
        c.blockers.append("no market data for this coin yet")
    if (volume_h1_usd or 0) < s.min_volume_h1_usd:
        c.blockers.append(f"1h volume ${volume_h1_usd or 0:,.0f} < ${s.min_volume_h1_usd:,.0f}")
    if (liquidity_usd or 0) < s.min_liquidity_usd:
        c.blockers.append(f"liquidity ${liquidity_usd or 0:,.0f} < ${s.min_liquidity_usd:,.0f}")
    if holders:
        _holder_checks(s, c, holders)
    # Volume now vs. average hour of the last day: >1 means heating up.
    hourly_avg = (volume_h24_usd or 0) / 24
    heat = (volume_h1_usd or 0) / hourly_avg if hourly_avg > 0 else 0.0
    c.metrics["volume_heat"] = round(heat, 2)
    liq_ratio = (volume_h1_usd or 0) / liquidity_usd if liquidity_usd else 0
    c.metrics["volume_to_liquidity_h1"] = round(liq_ratio, 2)
    if liq_ratio > 5:
        c.flags.append("1h volume > 5x liquidity: possible wash trading")
    c.score = round(min(heat, 5) + min(liq_ratio, 2) - len(c.flags) * 0.5, 3)
    return c


def _quote_check(c: Candidate, quote_symbol: str) -> None:
    """Risk limits and position sizing are in SOL, so only SOL-priced trades are supported."""
    c.metrics["quote"] = quote_symbol
    if quote_symbol != QUOTE_SYMBOL:
        c.blockers.append(f"priced in {quote_symbol}; only {QUOTE_SYMBOL} trades are supported")


def _holder_checks(s: Settings, c: Candidate, h: dict[str, Any]) -> None:
    top10, top, creator = h.get("top10_share"), h.get("top_holder_share"), h.get("creator_share")
    c.metrics.update({k: h[k] for k in ("top10_share", "top_holder_share", "creator_share", "wallets_counted")
                      if h.get(k) is not None})
    if creator is not None and creator > s.max_deployer_hold_share:
        c.blockers.append(f"the creator still holds {creator:.0%} of the supply")
    if top10 is not None and top10 > s.max_top10_hold_share:
        c.blockers.append(f"the 10 biggest wallets hold {top10:.0%} of the supply")
    if top is not None and top > s.max_top_buyer_share:
        c.flags.append(f"one wallet holds {top:.0%} of the supply")
