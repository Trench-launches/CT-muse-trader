"""Indicative market data from DEX aggregators.

  * aggregator numbers are indicative, never an executable quote (swaps get a real Jupiter quote);
  * a coin's stats come from its deepest SOL-quoted pool (Jupiter routes across every pool, so there is no
    single "genuine" pool to match the way there was on Pons);
  * prices are in SOL or USD and labelled as such; USD only through an explicit price source.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from ..chain.keys import SOL_MINT

log = logging.getLogger(__name__)

DEXSCREENER = "https://api.dexscreener.com"
GECKOTERMINAL = "https://api.geckoterminal.com/api/v2"
COINGECKO = "https://api.coingecko.com/api/v3"
CURVE_DEXES = {"pumpfun", "moonshot", "launchlab", "boop", "believe"}  # still on a launchpad bonding curve


@dataclass(frozen=True)
class PoolStats:
    source: str
    pool_id: str
    price_usd: float | None
    price_native: float | None  # SOL per token
    volume_h1_usd: float | None
    volume_h24_usd: float | None
    liquidity_usd: float | None
    txns_h1: int | None
    raw: dict[str, Any]
    dex_id: str = ""
    market_cap_usd: float | None = None

    @property
    def on_curve(self) -> bool:
        return self.dex_id in CURVE_DEXES


def _f(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _from_dexscreener(p: dict[str, Any]) -> PoolStats:
    vol, liq, tx = p.get("volume") or {}, p.get("liquidity") or {}, (p.get("txns") or {}).get("h1") or {}
    return PoolStats(
        source="dexscreener", pool_id=str(p.get("pairAddress") or ""), price_usd=_f(p.get("priceUsd")),
        price_native=_f(p.get("priceNative")),
        volume_h1_usd=_f(vol.get("h1")), volume_h24_usd=_f(vol.get("h24")),
        liquidity_usd=_f(liq.get("usd")),
        txns_h1=(int(tx.get("buys") or 0) + int(tx.get("sells") or 0)) if tx else None,  # APIs send null, not 0
        raw=p, dex_id=str(p.get("dexId") or ""), market_cap_usd=_f(p.get("marketCap") or p.get("fdv")),
    )


class MarketData:
    def __init__(self, *, dexscreener_chain: str = "solana", gt_network: str = "solana",
                 transport: httpx.BaseTransport | None = None, timeout: float = 15.0):
        self.ds_chain = dexscreener_chain
        self.gt_network = gt_network
        self._http = httpx.Client(timeout=timeout, transport=transport,
                                  headers={"accept": "application/json", "user-agent": "vigil/0.2"})
        self._sol_usd: tuple[float, float] | None = None  # (price, fetched_at)

    def close(self) -> None:
        self._http.close()

    # --- pool stats -----------------------------------------------------------

    def pairs(self, mint: str) -> list[dict[str, Any]]:
        """Every DexScreener pair of a mint where it is the base token."""
        r = self._http.get(f"{DEXSCREENER}/token-pairs/v1/{self.ds_chain}/{mint}")
        r.raise_for_status()
        return [p for p in (r.json() or []) if (p.get("baseToken") or {}).get("address") == mint]

    def pool_stats(self, mint: str) -> PoolStats | None:
        """The coin's deepest SOL-quoted pool. None when there is none (or the feeds are down)."""
        try:
            sol_pairs = [p for p in self.pairs(mint) if (p.get("quoteToken") or {}).get("address") == SOL_MINT]
            if sol_pairs:
                return _from_dexscreener(max(sol_pairs, key=lambda p: _f((p.get("liquidity") or {}).get("usd")) or 0))
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:  # bad data skips this token only
            log.warning("dexscreener failed for %s: %s", mint, exc)
        try:
            return self._geckoterminal(mint)
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
            log.warning("geckoterminal failed for %s: %s", mint, exc)
        return None

    def prices_usd(self, mints: list[str]) -> dict[str, float]:
        """USD price per mint from DexScreener (up to 30 a call), for watching many calls at once."""
        out: dict[str, float] = {}
        uniq = list(dict.fromkeys(m for m in mints if m))
        for i in range(0, len(uniq), 30):
            try:
                r = self._http.get(f"{DEXSCREENER}/tokens/v1/{self.ds_chain}/{','.join(uniq[i:i + 30])}")
                r.raise_for_status()
            except httpx.HTTPError as exc:
                log.warning("dexscreener prices failed: %s", exc)
                continue
            best: dict[str, tuple[float, float]] = {}
            for p in r.json() or []:
                mint = (p.get("baseToken") or {}).get("address")
                px, liq = _f(p.get("priceUsd")), _f((p.get("liquidity") or {}).get("usd")) or 0
                if mint in uniq and px and liq >= best.get(mint, (0, -1))[1]:
                    best[mint] = (px, liq)
            out.update({m: v[0] for m, v in best.items()})
        return out

    def _geckoterminal(self, mint: str) -> PoolStats | None:
        r = self._http.get(f"{GECKOTERMINAL}/networks/{self.gt_network}/tokens/{mint}/pools", params={"page": 1})
        if r.status_code == 404:
            return None
        r.raise_for_status()
        pools = [d.get("attributes") or {} for d in (r.json().get("data") or [])]
        if not pools:
            return None
        a = max(pools, key=lambda x: _f(x.get("reserve_in_usd")) or 0)
        vol = a.get("volume_usd") or {}
        tx = (a.get("transactions") or {}).get("h1") or {}
        return PoolStats(
            source="geckoterminal", pool_id=str(a.get("address") or ""), price_usd=_f(a.get("base_token_price_usd")),
            price_native=_f(a.get("base_token_price_native_currency") or a.get("base_token_price_quote_token")),
            volume_h1_usd=_f(vol.get("h1")), volume_h24_usd=_f(vol.get("h24")),
            liquidity_usd=_f(a.get("reserve_in_usd")),
            txns_h1=(int(tx.get("buys") or 0) + int(tx.get("sells") or 0)) if tx else None,
            raw=a, market_cap_usd=_f(a.get("market_cap_usd") or a.get("fdv_usd")),
        )

    # --- quote conversion -----------------------------------------------------

    def sol_usd(self, max_age_s: float = 300) -> float | None:
        """Indicative SOL/USD, cached. None if unavailable; callers then report amounts in SOL instead of
        inventing a USD figure."""
        now = time.time()
        if self._sol_usd and now - self._sol_usd[1] < max_age_s:
            return self._sol_usd[0]
        try:
            r = self._http.get(f"{COINGECKO}/simple/price", params={"ids": "solana", "vs_currencies": "usd"})
            r.raise_for_status()
            price = float(r.json()["solana"]["usd"])
        except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
            log.warning("sol/usd unavailable: %s", exc)
            return self._sol_usd[0] if self._sol_usd else None
        self._sol_usd = (price, now)
        return price
