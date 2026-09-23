"""Indicative market data from DEX aggregators.

Rules from the brief:
  * aggregator numbers are indicative, never a live executable quote;
  * only the genuine graduated Pons pool counts (matched by V4 pool id),
    lookalike pools for the same token are ignored;
  * quote units are ETH or an ERC-20, converted to USD only through an
    explicit, labelled price source.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)

DEXSCREENER = "https://api.dexscreener.com"
GECKOTERMINAL = "https://api.geckoterminal.com/api/v2"
COINGECKO = "https://api.coingecko.com/api/v3"


@dataclass(frozen=True)
class PoolStats:
    source: str
    pool_id: str
    price_usd: float | None
    price_native: float | None  # in the pool's quote asset
    volume_h1_usd: float | None
    volume_h24_usd: float | None
    liquidity_usd: float | None
    txns_h1: int | None
    raw: dict[str, Any]


def _f(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


class MarketData:
    def __init__(self, *, dexscreener_chain: str = "robinhood", gt_network: str = "robinhood",
                 transport: httpx.BaseTransport | None = None, timeout: float = 15.0):
        self.ds_chain = dexscreener_chain
        self.gt_network = gt_network
        self._http = httpx.Client(timeout=timeout, transport=transport,
                                  headers={"accept": "application/json", "user-agent": "ct-agent/0.1"})
        self._eth_usd: tuple[float, float] | None = None  # (price, fetched_at)

    def close(self) -> None:
        self._http.close()

    # --- genuine pool stats -------------------------------------------------

    def genuine_pool_stats(self, token: str, pool_id: str) -> PoolStats | None:
        """Stats for the one pool whose id matches the Pons-seeded pool.
        Returns None rather than falling back to a lookalike."""
        pid = pool_id.lower()
        try:
            stats = self._dexscreener(token, pid)
            if stats:
                return stats
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("dexscreener failed for %s: %s", token, exc)
        try:
            return self._geckoterminal(pid)
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("geckoterminal failed for %s: %s", pid, exc)
        return None

    def _dexscreener(self, token: str, pid: str) -> PoolStats | None:
        r = self._http.get(f"{DEXSCREENER}/tokens/v1/{self.ds_chain}/{token}")
        r.raise_for_status()
        for p in r.json() or []:
            if str(p.get("pairAddress", "")).lower() == pid:
                vol, liq, tx = p.get("volume") or {}, p.get("liquidity") or {}, (p.get("txns") or {}).get("h1") or {}
                return PoolStats(
                    source="dexscreener", pool_id=pid, price_usd=_f(p.get("priceUsd")),
                    price_native=_f(p.get("priceNative")),
                    volume_h1_usd=_f(vol.get("h1")), volume_h24_usd=_f(vol.get("h24")),
                    liquidity_usd=_f(liq.get("usd")),
                    txns_h1=(int(tx.get("buys", 0)) + int(tx.get("sells", 0))) if tx else None,
                    raw=p,
                )
        return None

    def _geckoterminal(self, pid: str) -> PoolStats | None:
        r = self._http.get(f"{GECKOTERMINAL}/networks/{self.gt_network}/pools/{pid}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        a = (r.json().get("data") or {}).get("attributes") or {}
        if str(a.get("address", "")).lower() != pid:
            return None
        vol = a.get("volume_usd") or {}
        tx = (a.get("transactions") or {}).get("h1") or {}
        return PoolStats(
            source="geckoterminal", pool_id=pid, price_usd=_f(a.get("base_token_price_usd")),
            price_native=_f(a.get("base_token_price_quote_token")),
            volume_h1_usd=_f(vol.get("h1")), volume_h24_usd=_f(vol.get("h24")),
            liquidity_usd=_f(a.get("reserve_in_usd")),
            txns_h1=(int(tx.get("buys", 0)) + int(tx.get("sells", 0))) if tx else None,
            raw=a,
        )

    # --- quote conversion ---------------------------------------------------

    def eth_usd(self, max_age_s: float = 300) -> float | None:
        """Indicative ETH/USD, cached. None if unavailable; callers must then
        report amounts in ETH instead of inventing a USD figure."""
        now = time.time()
        if self._eth_usd and now - self._eth_usd[1] < max_age_s:
            return self._eth_usd[0]
        try:
            r = self._http.get(f"{COINGECKO}/simple/price", params={"ids": "ethereum", "vs_currencies": "usd"})
            r.raise_for_status()
            price = float(r.json()["ethereum"]["usd"])
        except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
            log.warning("eth/usd unavailable: %s", exc)
            return self._eth_usd[0] if self._eth_usd else None
        self._eth_usd = (price, now)
        return price
