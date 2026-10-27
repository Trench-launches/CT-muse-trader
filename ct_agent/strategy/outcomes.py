"""Call outcomes: what happened to every token HORIZON made a call on.

Each decided token (buy, watch or pass) is recorded once per 24h with the
variables HORIZON saw at that moment and its spot price. The price is then
re-read at fixed checkpoints, so the report can show which variables were
followed by the token going up, and which by it dying.

Returns here are raw spot-price moves, not trade P&L: no fees, no price
impact. Paper trade P&L (with fees) lives in positions.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from ..chain.pons import Phase
from ..db import rows, utcnow

CHECKPOINTS = {"p15m": 15, "p1h": 60, "p4h": 240, "p24h": 1440}  # minutes after the call


def spot_price(ctx: Any, token: str) -> tuple[float | None, str]:
    """Spot price in ETH per token: curve reserves before graduation, the
    genuine Pons pool after. (None, why) when there is no price."""
    rec = ctx.pons.launch_record(token)
    launch = ctx.conn.execute("SELECT decimals, quote_decimals, pool_id FROM launches WHERE token=?",
                              (token,)).fetchone()
    td = (launch["decimals"] if launch and launch["decimals"] is not None else 18)
    qd = (launch["quote_decimals"] if launch and launch["quote_decimals"] is not None else 18)
    if rec.phase == Phase.NOT_GRADUATED:
        q, t = ctx.pons.curve_reserves(rec.curve)
        return ((q / 10 ** qd) / (t / 10 ** td), "curve") if t else (None, "empty curve")
    if rec.phase == Phase.POOL_CREATED:
        pid = (launch["pool_id"] if launch and launch["pool_id"] else None) or ctx.pons.genuine_pool_id(rec)
        pool = ctx.market.genuine_pool_stats(token, pid)
        return (pool.price_native, "pool") if pool and pool.price_native else (None, "no pool price")
    return None, f"phase {rec.phase.name}"


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
        "stage": a.cand.stage, "progress": a.cand.progress, "score": a.cand.score,
        "flags": a.cand.flags, "metrics": a.cand.metrics,
        "fee_bps": (a.launch.get("fee_bps") or 0) + (a.record.creator_tax_bps or 0),
        "x_attention_24h": (pack.get("x_attention_24h") or {}).get("count_24h"),
        "evidence_count": len(ev), "evidence_grades": grades,
        "ai_confidence": confidence, "model": model, "gates": gates,
        "pool_liquidity_usd": a.pool.liquidity_usd if a.pool else None,
        "pool_volume_h1_usd": a.pool.volume_h1_usd if a.pool else None,
    }
    conn.execute("INSERT INTO call_outcomes(decision_id,token,symbol,stage,action,decided_at,venue0,price0,features) "
                 "VALUES(?,?,?,?,?,?,?,?,?)",
                 (decision_id, a.cand.token, a.launch.get("symbol"), a.cand.stage, action, utcnow(), venue, price,
                  json.dumps(features, default=str)))
    return True


def track_outcomes(ctx: Any, limit: int = 40) -> int:
    """Fill in whichever checkpoints are now due. Bounded per cycle so a
    backlog can't flood the RPC."""
    conn, now = ctx.conn, datetime.now(timezone.utc)
    filled = 0
    for r in rows(conn, "SELECT * FROM call_outcomes WHERE done=0 AND price0 IS NOT NULL ORDER BY decided_at LIMIT ?",
                  (limit,)):
        age_min = (now - datetime.fromisoformat(r["decided_at"])).total_seconds() / 60
        due = [c for c, m in CHECKPOINTS.items() if r[c] is None and age_min >= m]
        if not due:
            continue
        try:
            price, venue = spot_price(ctx, r["token"])
        except Exception:
            price, venue = None, "price error"
        # A token with no price any more (dead curve, pool gone) is recorded as 0.
        p = price if price is not None else 0.0
        sets = {c: p for c in due}
        sets["peak"] = max(r["peak"] or r["price0"], p)
        sets["trough"] = min(r["trough"] if r["trough"] is not None else r["price0"], p)
        sets["graduated"] = int(bool(r["graduated"]) or venue == "pool")
        sets["done"] = int(r["p24h"] is not None or "p24h" in due)
        conn.execute(f"UPDATE call_outcomes SET {', '.join(f'{k}=?' for k in sets)} WHERE decision_id=?",
                     (*sets.values(), r["decision_id"]))
        filled += 1
    return filled


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
        "curve progress": _band(f.get("progress"), [0.9, 0.95, 2], ["85-90%", "90-95%", "95%+"])
        if r["stage"] == "almost_bonded" else "n/a (graduated)",
        "screen score": _band(f.get("score"), [2, 4, 6, 1e9], ["<2", "2-4", "4-6", "6+"]),
        "X mentions 24h": _band(f.get("x_attention_24h"), [1, 6, 21, 1e12], ["0", "1-5", "6-20", "21+"]),
        "evidence": "buy-grade" if grades & {"verified", "first_party", "reported"}
        else ("rumour/chat only" if grades else "none"),
        "AI confidence": _band(f.get("ai_confidence", f.get("muse_confidence")), [0.5, 0.7, 0.85, 2], ["<0.5", "0.5-0.7", "0.7-0.85", "0.85+"]),
        "whale flag": "yes" if "one wallet is" in flags else "no",
        "wash flag": "yes" if ("wash" in flags) else "no",
        "creator tax": "yes" if "creator tax" in flags else "no",
        "buys accelerating": "yes" if m.get("accelerating") else "no",
        "fee+tax": _band(f.get("fee_bps"), [150, 400, 1e9], ["<1.5%", "1.5-4%", "4%+"]),
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
