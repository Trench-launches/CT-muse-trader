"""The `tokens` cache: every Solana coin VIGIL has looked at, filled on demand.

Decimals and the token program never change, so they are read once. Where the coin trades (still on a launchpad
bonding curve, or graduated to a pool, and on which DEX) is refreshed every few minutes from the market data.
Mints are base58 and case-sensitive: they are stored and matched exactly as given.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from ..chain import token as tok
from ..chain.keys import is_pubkey
from ..db import utcnow

log = logging.getLogger(__name__)
REFRESH_S = 600


def get(conn: Any, mint: str) -> dict[str, Any] | None:
    r = conn.execute("SELECT * FROM tokens WHERE mint=?", (mint,)).fetchone()
    return dict(r) if r else None


def symbols(conn: Any, mints: list[str]) -> dict[str, str]:
    """{mint: symbol} for the mints the cache knows."""
    mints = [m for m in dict.fromkeys(mints) if m]
    if not mints:
        return {}
    q = ",".join("?" * len(mints))
    return {r["mint"]: r["symbol"] for r in conn.execute(f"SELECT mint, symbol FROM tokens WHERE mint IN ({q})", mints)}


def stage_of(pool: Any) -> str | None:
    """'curve' while the coin is on a launchpad bonding curve, 'pool' once it trades in a pool, None if unknown."""
    if pool is None:
        return None
    return "curve" if pool.on_curve else "pool"


def _stale(row: dict[str, Any]) -> bool:
    try:
        return datetime.now(timezone.utc) - datetime.fromisoformat(row["last_checked"]) > timedelta(seconds=REFRESH_S)
    except (TypeError, ValueError):
        return True


def ensure(ctx: Any, mint: str, *, info: tok.MintInfo | None = None, pool: Any = None,
           refresh: bool = False) -> dict[str, Any] | None:
    """The cache row for `mint`, created from the chain and the market data the first time (None if it isn't a token
    mint). `info` and `pool` can be passed in when the caller already read them, to save the calls."""
    if not is_pubkey(mint):
        return None
    conn = ctx.conn
    row = get(conn, mint)
    if row and not refresh and not _stale(row) and pool is None:
        return row
    if row is None:
        info = info or tok.mint_info(ctx.rpc, mint)
        if not info.exists:
            return None
    if pool is None:
        try:
            pool = ctx.market.pool_stats(mint)
        except Exception as exc:  # market data is extra: the row still gets its decimals
            log.warning("pool stats unavailable for %s: %s", mint, exc)
    base = (pool.raw.get("baseToken") or {}) if pool is not None and isinstance(pool.raw, dict) else {}
    stage, dex = stage_of(pool), (pool.dex_id if pool is not None else None) or None
    pool_id = (pool.pool_id if pool is not None else None) or None
    now = utcnow()
    if row is None:
        meta: dict[str, Any] = {}
        try:
            meta = tok.token_meta(ctx.rpc, mint)
        except Exception as exc:
            log.info("token metadata unavailable for %s: %s", mint, exc)
        info_img = ((pool.raw.get("info") or {}).get("imageUrl") if pool is not None and isinstance(pool.raw, dict)
                    else None)
        conn.execute("INSERT OR IGNORE INTO tokens(mint,name,symbol,decimals,program,stage,dex_id,pool_id,logo_url,creator,"
                     "first_seen,last_checked) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                     (mint, (meta.get("name") or base.get("name") or "")[:80] or None,
                      (meta.get("symbol") or base.get("symbol") or "")[:20] or None, info.decimals, info.program,
                      stage, dex, pool_id, (meta.get("image") or info_img or "")[:500], None, now, now))
    else:
        conn.execute("UPDATE tokens SET stage=COALESCE(?, stage), dex_id=COALESCE(?, dex_id), pool_id=COALESCE(?, pool_id), "
                     "symbol=COALESCE(symbol, ?), name=COALESCE(name, ?), last_checked=? WHERE mint=?",
                     (stage, dex, pool_id, base.get("symbol") or None, base.get("name") or None, now, mint))
    return get(conn, mint)


def decimals(ctx: Any, mint: str) -> int:
    row = get(ctx.conn, mint) or ensure(ctx, mint)
    if not row:
        raise ValueError(f"{mint} is not a token mint")
    return int(row["decimals"])
