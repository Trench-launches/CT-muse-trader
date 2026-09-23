"""Fill estimates for paper trades and live minOut calculation.

Curve: exact constant-product math against getReserves(), with the fee and
creator tax taken from the quote leg exactly as PonsV2BondingCurve does.
Pool: an estimate from indicative liquidity, clearly labelled.
"""
from __future__ import annotations

from dataclasses import dataclass

BPS = 10_000


@dataclass(frozen=True)
class FillEstimate:
    quote_in: float
    tokens_out: float
    avg_price: float          # quote per token
    spot_price: float         # quote per token before the trade
    price_impact_bps: int
    fee_and_tax_quote: float
    method: str


def curve_buy(quote_in_wei: int, quote_reserve: int, token_reserve: int, fee_bps: int, tax_bps: int,
              quote_decimals: int = 18, token_decimals: int = 18) -> FillEstimate:
    fee = quote_in_wei * (fee_bps + tax_bps) // BPS
    net = quote_in_wei - fee
    out = token_reserve * net // (quote_reserve + net)
    qs, ts = 10 ** quote_decimals, 10 ** token_decimals
    spot = (quote_reserve / qs) / (token_reserve / ts)
    avg = (quote_in_wei / qs) / (out / ts) if out else float("inf")
    # Price impact is how far the trade itself moves the price (net of fees);
    # fees and creator tax are reported and limited separately.
    impact = int(((net / qs) / (out / ts) / spot - 1) * BPS) if out else BPS
    return FillEstimate(quote_in_wei / qs, out / ts, avg, spot, impact, fee / qs, "curve_constant_product")


def curve_sell(tokens_in_wei: int, quote_reserve: int, token_reserve: int, fee_bps: int, tax_bps: int,
               quote_decimals: int = 18, token_decimals: int = 18) -> FillEstimate:
    gross = quote_reserve * tokens_in_wei // (token_reserve + tokens_in_wei)
    fee = gross * (fee_bps + tax_bps) // BPS
    net = gross - fee
    qs, ts = 10 ** quote_decimals, 10 ** token_decimals
    spot = (quote_reserve / qs) / (token_reserve / ts)
    avg = (net / qs) / (tokens_in_wei / ts) if tokens_in_wei else 0.0
    gross_avg = (gross / qs) / (tokens_in_wei / ts) if tokens_in_wei else 0.0
    impact = int((1 - gross_avg / spot) * BPS) if spot else BPS
    return FillEstimate(net / qs, tokens_in_wei / ts, avg, spot, impact, fee / qs, "curve_constant_product")


def pool_estimate(quote_in: float, price_native: float, liquidity_usd: float, eth_usd: float,
                  fee_bps: int, buy: bool = True) -> FillEstimate:
    """Approximate a full-range V4 pool as constant product with half the
    liquidity on each side. Indicative only."""
    r_q = (liquidity_usd / 2) / eth_usd
    r_t = r_q / price_native
    if buy:
        net = quote_in * (1 - fee_bps / BPS)
        out = r_t * net / (r_q + net)
        avg = quote_in / out
        return FillEstimate(quote_in, out, avg, price_native, int((net / out / price_native - 1) * BPS),
                            quote_in - net, "pool_estimate_indicative")
    tokens = quote_in  # for sells, first arg is token amount
    gross = r_q * tokens / (r_t + tokens)
    net = gross * (1 - fee_bps / BPS)
    avg = net / tokens
    return FillEstimate(net, tokens, avg, price_native, int((1 - gross / tokens / price_native) * BPS), gross - net,
                        "pool_estimate_indicative")
