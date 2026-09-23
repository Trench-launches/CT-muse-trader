"""Prototype candidate filters from the brief. Not a profitable strategy.

Stages:
  almost_bonded  phase 0 and realQuoteReserve / graduationThreshold >= 0.85
  migrated       phase 2 (genuine Pons V4 pool), >= $5k 1h volume and
                 >= $15k liquidity on that pool, for paper screening
Skipped: phase 1 (swept), phase 3 (rescued), anything not a verified
Pons V2 launch, and anything still inside the launch snipe-tax window.
"""
from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from ..config import Settings
from ..db import rows


@dataclass
class WindowStats:
    seconds: int
    buys: int
    sells: int
    buy_quote: float
    sell_quote: float
    unique_buyers: int

    @property
    def buys_per_min(self) -> float:
        return self.buys / (self.seconds / 60)


@dataclass
class Activity:
    windows: list[WindowStats]
    top_buyer_share: float
    deployer_buy_share: float
    roundtrip_traders: int
    accelerating: bool
    acceleration: float


@dataclass
class Candidate:
    token: str
    stage: str
    progress: float | None
    phase: int
    score: float
    flags: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def eligible(self) -> bool:
        return not self.blockers


def activity_for(conn: sqlite3.Connection, token: str, deployer: str, head_block: int, block_time_s: float,
                 windows_s: list[int], quote_decimals: int = 18) -> Activity:
    longest = max(windows_s)
    lo = head_block - int(longest / block_time_s)
    trades = rows(conn, "SELECT * FROM curve_trades WHERE token=? AND block_number>=? ORDER BY block_number",
                  (token, lo))
    scale = 10 ** quote_decimals
    stats = []
    for w in sorted(windows_s):
        start = head_block - int(w / block_time_s)
        ts = [t for t in trades if t["block_number"] >= start]
        b = [t for t in ts if t["side"] == "buy"]
        s = [t for t in ts if t["side"] == "sell"]
        stats.append(WindowStats(w, len(b), len(s), sum(int(t["quote_amount"]) for t in b) / scale,
                                 sum(int(t["quote_amount"]) for t in s) / scale,
                                 len({t["trader"] for t in b})))
    buys = [t for t in trades if t["side"] == "buy"]
    by_buyer = Counter()
    for t in buys:
        by_buyer[t["trader"].lower()] += int(t["quote_amount"])
    total = sum(by_buyer.values()) or 1
    top_share = (max(by_buyer.values()) / total) if by_buyer else 0.0
    dep_share = by_buyer.get(deployer.lower(), 0) / total
    sellers = {t["trader"].lower() for t in trades if t["side"] == "sell"}
    roundtrips = len(sellers & set(by_buyer))
    # Acceleration only counts if every shorter window beats the next longer
    # one, so a single burst in one window is not trusted.
    rates = [w.buys_per_min for w in stats]
    accelerating = len(rates) >= 2 and all(rates[i] > rates[i + 1] for i in range(len(rates) - 1)) and stats[0].buys >= 3
    accel = rates[0] / rates[-1] if rates and rates[-1] > 0 else (float(rates[0] > 0) if rates else 0.0)
    return Activity(stats, top_share, dep_share, roundtrips, accelerating, accel)


def screen_almost_bonded(s: Settings, *, token: str, progress: float, phase: int, launch_age_s: float,
                         snipe_window_s: int, fee_bps: int, creator_tax_bps: int, activity: Activity,
                         quote_symbol: str = "ETH") -> Candidate:
    c = Candidate(token=token, stage="almost_bonded", progress=progress, phase=phase, score=0.0)
    _quote_check(c, quote_symbol)
    if phase != 0:
        c.blockers.append(f"phase {phase} is not the curve phase")
    if progress < s.almost_bonded_ratio:
        c.blockers.append(f"progress {progress:.2f} < {s.almost_bonded_ratio}")
    if launch_age_s <= snipe_window_s:
        c.blockers.append("inside launch snipe-tax window")
    _common_checks(s, c, fee_bps, creator_tax_bps, activity)
    c.score = round(progress * 2 + min(activity.acceleration, 5) + (2 if activity.accelerating else 0)
                    - len(c.flags) * 0.5, 3)
    return c


def screen_migrated(s: Settings, *, token: str, phase: int, pool_found: bool, volume_h1_usd: float | None,
                    liquidity_usd: float | None, volume_h24_usd: float | None, fee_bps: int,
                    creator_tax_bps: int, activity: Activity | None, quote_symbol: str = "ETH") -> Candidate:
    c = Candidate(token=token, stage="migrated", progress=1.0, phase=phase, score=0.0)
    _quote_check(c, quote_symbol)
    if phase != 2:
        c.blockers.append(f"phase {phase} is not PoolCreated")
    if not pool_found:
        c.blockers.append("genuine Pons V4 pool not found in market data")
    if (volume_h1_usd or 0) < s.min_migrated_volume_h1_usd:
        c.blockers.append(f"1h volume ${volume_h1_usd or 0:,.0f} < ${s.min_migrated_volume_h1_usd:,.0f}")
    if (liquidity_usd or 0) < s.min_migrated_liquidity_usd:
        c.blockers.append(f"liquidity ${liquidity_usd or 0:,.0f} < ${s.min_migrated_liquidity_usd:,.0f}")
    if activity:
        _common_checks(s, c, fee_bps, creator_tax_bps, activity)
    else:
        _tax_check(s, c, fee_bps, creator_tax_bps)
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
    """Risk limits and position sizing are in ETH, so only ETH-priced tokens can be
    traded. Blocking here stops the AI being asked about untradeable tokens."""
    c.metrics["quote"] = quote_symbol
    if quote_symbol != "ETH":
        c.blockers.append(f"priced in {quote_symbol}; only ETH-priced tokens are supported for now")


def _tax_check(s: Settings, c: Candidate, fee_bps: int, creator_tax_bps: int) -> None:
    total = fee_bps + creator_tax_bps
    c.metrics["trade_fee_bps"] = total
    if total > s.max_trade_tax_bps:
        c.blockers.append(f"fee+tax {total} bps > {s.max_trade_tax_bps}")
    elif creator_tax_bps:
        c.flags.append(f"creator tax {creator_tax_bps} bps on every trade")


def _common_checks(s: Settings, c: Candidate, fee_bps: int, creator_tax_bps: int, a: Activity) -> None:
    _tax_check(s, c, fee_bps, creator_tax_bps)
    c.metrics.update({
        "top_buyer_share": round(a.top_buyer_share, 3),
        "deployer_buy_share": round(a.deployer_buy_share, 3),
        "roundtrip_traders": a.roundtrip_traders,
        "accelerating": a.accelerating,
        "acceleration": round(a.acceleration, 2),
        "windows": [w.__dict__ for w in a.windows],
    })
    if a.deployer_buy_share > s.max_deployer_buy_share:
        c.blockers.append(f"deployer bought {a.deployer_buy_share:.0%} of curve volume")
    if a.top_buyer_share > s.max_top_buyer_share:
        c.flags.append(f"one wallet is {a.top_buyer_share:.0%} of buys")
    if a.roundtrip_traders >= 3:
        c.flags.append(f"{a.roundtrip_traders} wallets both bought and sold: wash-trading clue")
