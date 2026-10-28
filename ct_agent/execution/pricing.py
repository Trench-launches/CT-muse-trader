"""Fill estimates for paper trades and position marks.

A paper buy or sell is priced from a real Jupiter quote whenever one is available (exactly what a swap would pay
right now, fees and price impact in). Without one, `pool_estimate` approximates the coin's pool from indicative
liquidity, clearly labelled as an estimate.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

BPS = 10_000


@dataclass(frozen=True)
class FillEstimate:
    quote_in: float           # SOL paid (buy) or received (sell)
    tokens_out: float         # tokens received (buy) or sold (sell)
    avg_price: float          # SOL per token
    spot_price: float         # SOL per token before the trade
    price_impact_bps: int
    fee_and_tax_quote: float
    method: str


def impact_bps(order: dict[str, Any]) -> int:
    """Price impact of a Jupiter order in basis points. `priceImpact` is a percentage; the older
    `priceImpactPct` is a fraction. Either sign means the same: how far the trade moves the price."""
    if order.get("priceImpact") not in (None, ""):
        return int(round(abs(float(order["priceImpact"])) * 100))
    if order.get("priceImpactPct") not in (None, ""):
        return int(round(abs(float(order["priceImpactPct"])) * BPS))
    return 0


def from_quote(order: dict[str, Any], *, buy: bool, decimals: int, method: str = "jupiter_quote") -> FillEstimate:
    """A paper fill from a Jupiter order: SOL in and tokens out for a buy, tokens in and SOL out for a sell."""
    a, b = int(order["inAmount"]), int(order["outAmount"])
    lamports, base = (a, b) if buy else (b, a)
    sol, tokens = lamports / 10 ** 9, base / 10 ** decimals
    avg = sol / tokens if tokens else 0.0
    imp = impact_bps(order)
    spot = avg / (1 + imp / BPS) if buy else avg / (1 - min(imp, BPS - 1) / BPS)
    return FillEstimate(sol, tokens, avg, spot, imp, 0.0, method)


def pool_estimate(quote_in: float, price_native: float, liquidity_usd: float, sol_usd: float,
                  fee_bps: int, buy: bool = True) -> FillEstimate:
    """Approximate the coin's pool as constant product with half the liquidity on each side. Indicative only.
    For a sell, the first argument is the token amount."""
    r_q = (liquidity_usd / 2) / sol_usd
    r_t = r_q / price_native
    if buy:
        net = quote_in * (1 - fee_bps / BPS)
        out = r_t * net / (r_q + net)
        avg = quote_in / out
        return FillEstimate(quote_in, out, avg, price_native, int((net / out / price_native - 1) * BPS),
                            quote_in - net, "pool_estimate_indicative")
    tokens = quote_in
    gross = r_q * tokens / (r_t + tokens)
    net = gross * (1 - fee_bps / BPS)
    avg = net / tokens
    return FillEstimate(net, tokens, avg, price_native, int((1 - gross / tokens / price_native) * BPS), gross - net,
                        "pool_estimate_indicative")
