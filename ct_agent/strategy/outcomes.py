"""Call outcomes: what happened to every token VIGIL made a call on.

Each decided token (buy, watch or pass) is recorded once per 24h with the
variables VIGIL saw at that moment and its spot price. The price is then
re-read at fixed checkpoints, so the report can show which variables were
followed by the token going up, and which by it dying.

Returns here are raw spot-price moves, not trade P&L: no fees, no price
impact. Paper trade P&L (with fees) lives in positions.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from ..chain.keys import SOL_MINT
from ..db import rows, utcnow

log = logging.getLogger(__name__)

CHECKPOINTS = {"p15m": 15, "p1h": 60, "p4h": 240, "p24h": 1440}  # minutes after the call


def spot_prices(ctx: Any, mints: list[str]) -> dict[str, float]:
    """Spot price in SOL per token for each mint, in one batched call: Jupiter's price feed (USD per coin over USD
    per SOL), with DexScreener as the fallback. Mints with no price are left out."""
    mints = [m for m in dict.fromkeys(mints) if m]
    if not mints:
        return {}
    out: dict[str, float] = {}
    try:
        px = ctx.jupiter.prices(mints + [SOL_MINT])
        sol = float((px.get(SOL_MINT) or {}).get("usdPrice") or 0)
        if sol > 0:
            out = {m: float(px[m]["usdPrice"]) / sol for m in mints if m in px and px[m].get("usdPrice")}
    except Exception as exc:  # Jupiter down or rate limited: DexScreener below
        log.warning("jupiter prices unavailable: %s", exc)
    missing = [m for m in mints if m not in out]
    if missing:
        try:
            sol = ctx.market.sol_usd()
            if sol:
                out.update({m: p / sol for m, p in ctx.market.prices_usd(missing).items() if p})
        except Exception as exc:
            log.warning("dexscreener prices unavailable: %s", exc)
    return out


def spot_price(ctx: Any, token: str) -> tuple[float | None, str]:
    """Spot price in SOL per token, and where the coin trades ('curve' or 'pool', from the token cache).
    (None, why) when there is no price."""
    price = spot_prices(ctx, [token]).get(token)
    row = ctx.conn.execute("SELECT stage FROM tokens WHERE mint=?", (token,)).fetchone()
    venue = (row["stage"] if row and row["stage"] else None) or "market"
    return (price, venue) if price else (None, "no price")


def record_call(ctx: Any, a: Any, *, decision_id: int, action: str, confidence: float, pack: dict[str, Any],
                gates: list[str], model: str) -> bool:
    """Snapshot the variables behind a call. Once per token per 24h, so a token
    re-decided every cooldown doesn't count several times."""
    conn = ctx.conn
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
    if conn.execute("SELECT 1 FROM call_outcomes WHERE token=? AND decided_at>=?", (a.cand.token, since)).fetchone():
        return False
    try:
        price, venue = spot_price(ctx, a.cand.token)
    except Exception as exc:  # a missing price must never block trading
        price, venue = None, f"price error: {exc}"[:80]
    ev = pack.get("evidence") or []
    grades = sorted({e.get("confidence") for e in ev if e.get("confidence")})
    features = {
        "stage": a.cand.stage, "dex": a.coin.get("dex_id"), "score": a.cand.score,
        "flags": a.cand.flags, "blockers": a.cand.blockers, "metrics": a.cand.metrics,
        "x_attention_24h": (pack.get("x_attention_24h") or {}).get("count_24h"),
        "evidence_count": len(ev), "evidence_grades": grades,
        "ai_confidence": confidence, "model": model, "gates": gates,
        "pool_liquidity_usd": a.pool.liquidity_usd if a.pool else None,
        "pool_volume_h1_usd": a.pool.volume_h1_usd if a.pool else None,
    }
    conn.execute("INSERT INTO call_outcomes(decision_id,token,symbol,stage,action,decided_at,venue0,price0,features) "
                 "VALUES(?,?,?,?,?,?,?,?,?)",
                 (decision_id, a.cand.token, a.coin.get("symbol"), a.cand.stage or "unknown", action, utcnow(), venue, price,
                  json.dumps(features, default=str)))
    return True


def track_outcomes(ctx: Any, limit: int = 40) -> int:
    """Fill in whichever checkpoints are now due. Bounded per cycle, and priced in one batched call."""
    conn, now = ctx.conn, datetime.now(timezone.utc)
    due_rows = []
    for r in rows(conn, "SELECT * FROM call_outcomes WHERE done=0 AND price0 IS NOT NULL ORDER BY decided_at LIMIT ?",
                  (limit,)):
        age_min = (now - datetime.fromisoformat(r["decided_at"])).total_seconds() / 60
        due = [c for c, m in CHECKPOINTS.items() if r[c] is None and age_min >= m]
        if due:
            due_rows.append((r, due))
    if not due_rows:
        return 0
    try:
        prices = spot_prices(ctx, [r["token"] for r, _ in due_rows])
    except Exception:
        prices = {}
    mints = list({r["token"] for r, _ in due_rows})
    stages = {r["mint"]: r["stage"] for r in rows(conn, f"SELECT mint, stage FROM tokens WHERE mint IN "
                                                        f"({','.join('?' * len(mints))})", mints)}
    for r, due in due_rows:
        # A token with no price any more (dead, rugged, delisted) is recorded as 0.
        p = prices.get(r["token"]) or 0.0
        sets = {c: p for c in due}
        sets["peak"] = max(r["peak"] or r["price0"], p)
        sets["trough"] = min(r["trough"] if r["trough"] is not None else r["price0"], p)
        sets["graduated"] = int(bool(r["graduated"]) or (r["stage"] == "curve" and stages.get(r["token"]) == "pool"))
        sets["done"] = int(r["p24h"] is not None or "p24h" in due)
        conn.execute(f"UPDATE call_outcomes SET {', '.join(f'{k}=?' for k in sets)} WHERE decision_id=?",
                     (*sets.values(), r["decision_id"]))
    return len(due_rows)


# --- report -------------------------------------------------------------------

def _ret(r: dict[str, Any], col: str) -> float | None:
    return (r[col] / r["price0"] - 1) * 100 if r.get(col) is not None and r.get("price0") else None


def _band(v: float | None, edges: list[float], labels: list[str]) -> str:
    if v is None:
        return "unknown"
    for e, lab in zip(edges, labels):
        if v < e:
            return lab
    return labels[-1]


def _groups(r: dict[str, Any]) -> dict[str, str]:
    f = r["features"]
    m = f.get("metrics") or {}
    grades = set(f.get("evidence_grades") or [])
    flags = " ".join(f.get("flags") or [])
    return {
        "action": r["action"],
        "stage": r["stage"],
        "dex": f.get("dex") or "unknown",
        "screen score": _band(f.get("score"), [2, 4, 6, 1e9], ["<2", "2-4", "4-6", "6+"]),
        "X mentions 24h": _band(f.get("x_attention_24h"), [1, 6, 21, 1e12], ["0", "1-5", "6-20", "21+"]),
        "evidence": "buy-grade" if grades & {"verified", "first_party", "reported"}
        else ("rumour/chat only" if grades else "none"),
        "AI confidence": _band(f.get("ai_confidence", f.get("muse_confidence")), [0.5, 0.7, 0.85, 2], ["<0.5", "0.5-0.7", "0.7-0.85", "0.85+"]),
        "whale flag": "yes" if "one wallet holds" in flags else "no",
        "wash flag": "yes" if ("wash" in flags) else "no",
        "screen": "passed" if not f.get("blockers") else "failed",
        "volume heat": _band(m.get("volume_heat"), [1, 2, 4, 1e9], ["<1x", "1-2x", "2-4x", "4x+"]),
        "top 10 holders": _band(m.get("top10_share"), [0.15, 0.25, 0.35, 2], ["<15%", "15-25%", "25-35%", "35%+"]),
    }


def report(conn: Any, hours: float | None = None) -> dict[str, Any]:
    """Average move after the call, overall and split by each variable.
    'hit' = peak at a checkpoint reached +25%; 'dead' = latest price is down 50%+."""
    q = "SELECT * FROM call_outcomes WHERE price0 IS NOT NULL AND p15m IS NOT NULL"
    args: tuple = ()
    if hours:
        q += " AND decided_at>=?"
        args = ((datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds"),)
    data = rows(conn, q, args)
    for r in data:
        r["features"] = json.loads(r["features"] or "{}")
        latest = next((r[c] for c in ("p24h", "p4h", "p1h", "p15m") if r[c] is not None), None)
        r["_latest"] = (latest / r["price0"] - 1) * 100 if latest is not None else None
        r["_peak"] = _ret(r, "peak")

    def summarise(items: list[dict[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {"n": len(items)}
        for c in CHECKPOINTS:
            vals = [v for v in (_ret(r, c) for r in items) if v is not None]
            out[f"avg_{c[1:]}"] = round(sum(vals) / len(vals), 1) if vals else None
        out["hit_rate_+25%"] = round(100 * sum(1 for r in items if (r["_peak"] or 0) >= 25) / len(items)) if items else None
        out["dead_rate"] = round(100 * sum(1 for r in items if (r["_latest"] or 0) <= -50) / len(items)) if items else None
        out["graduated"] = sum(1 for r in items if r.get("graduated"))
        return out

    by: dict[str, dict[str, Any]] = {}
    for r in data:
        for var, bucket in _groups(r).items():
            by.setdefault(var, {}).setdefault(bucket, []).append(r)
    return {
        "calls_tracked": len(data),
        "note": "raw spot-price moves after the call (no fees); hit = +25% at any checkpoint; dead = down 50%+",
        "overall": summarise(data),
        "by_variable": {var: {b: summarise(items) for b, items in sorted(buckets.items())}
                        for var, buckets in by.items()},
        "best_calls": [{"symbol": r["symbol"], "token": r["token"], "action": r["action"], "peak_pct": round(r["_peak"], 1)}
                       for r in sorted(data, key=lambda r: r["_peak"] or -1e9, reverse=True)[:5] if r["_peak"] is not None],
    }
