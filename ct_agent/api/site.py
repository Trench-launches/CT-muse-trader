"""Everything the public website shows, in one read-only payload.

The landing page polls GET /site. Figures are real: counts from the
indexer, the latest sources and onchain checks, Muse's actual reasoning,
open paper positions, posts that went out, and the paper scorecard.
Nothing here is secret; the API is public by design.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from ..config import Settings
from ..db import journal_verify, kv_get, rows
from ..execution.risk import RiskManager

_CACHE: dict[str, Any] = {}
CACHE_S = 15


def _since(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")


def _j(v: Any, default: Any) -> Any:
    try:
        return json.loads(v) if isinstance(v, str) else (v if v is not None else default)
    except ValueError:
        return default


def _symbols(conn: Any, tokens: list[str]) -> dict[str, str]:
    if not tokens:
        return {}
    q = ",".join("?" * len(tokens))
    return {r["token"].lower(): r["symbol"] for r in
            rows(conn, f"SELECT token, symbol FROM launches WHERE lower(token) IN ({q})", [t.lower() for t in tokens])}


def _source(r: dict[str, Any]) -> dict[str, Any]:
    return {"id": r["id"], "kind": r["kind"], "author": r["author"], "channel": r["channel"],
            "url": r["origin_url"], "at": r["published_at"] or r["captured_at"], "confidence": r["confidence"],
            "excerpt": (r["excerpt"] or "")[:220]}


def _journal_ok(conn: Any) -> bool:
    """Verifying the whole hash chain is slow on a big journal: re-check every 5 minutes."""
    c = _CACHE.get("journal")
    if c and time.time() - c[1] < 300:
        return c[0]
    ok = journal_verify(conn)[0]
    _CACHE["journal"] = (ok, time.time())
    return ok


def site_data(conn: Any, s: Settings) -> dict[str, Any]:
    hit = _CACHE.get("site")
    if hit and time.time() - hit[1] < CACHE_S:
        return hit[0]
    data = _build(conn, s)
    _CACHE["site"] = (data, time.time())
    return data


def _build(conn: Any, s: Settings) -> dict[str, Any]:
    one = lambda q, a=(): conn.execute(q, a).fetchone()[0]  # noqa: E731
    day = _since(24)
    risk = RiskManager(s, conn)
    killed, why = risk.kill_active()

    # --- volume funnel ---------------------------------------------------------
    beat = kv_get(conn, "indexer_heartbeat") or {}
    head = beat.get("head") or kv_get(conn, "launch_cursor") or 0
    bt = float((kv_get(conn, "block_time_s") or {}).get("value", 0.1)) or 0.1
    launched = one("SELECT COUNT(*) FROM launches WHERE block_number>=?", (head - int(86400 / bt),)) if head else 0
    funnel = {
        "launched_24h": launched,
        "near_graduation": one("SELECT COUNT(*) FROM launches WHERE last_phase=0 AND last_progress>=?",
                               (s.almost_bonded_ratio,)),
        "screened_24h": one("SELECT COUNT(DISTINCT token) FROM decisions WHERE ts>=?", (day,)),
        "traded_24h": one("SELECT COUNT(*) FROM positions WHERE mode=? AND opened_at>=?", (s.trading_mode.value, day)),
    }
    stats = {
        "claims_checked_24h": one("SELECT COUNT(*) FROM leads WHERE created_at>=? AND status!='unverified'", (day,)),
        "posts_sent": one("SELECT COUNT(*) FROM posts WHERE status='sent'"),
        "sources_24h": one("SELECT COUNT(*) FROM sources WHERE captured_at>=?", (day,)),
    }

    # --- listening: newest source of each kind ---------------------------------
    sources = []
    for kinds in (("x",), ("news",), ("twitch_transcript", "twitch_chat")):
        q = ",".join("?" * len(kinds))
        # Prefer a source whose contract was verified onchain: it's the one that matters.
        r = rows(conn, f"SELECT s.* FROM sources s JOIN leads l ON l.source_id=s.id WHERE s.kind IN ({q}) "
                       "AND l.status='verified' ORDER BY s.id DESC LIMIT 1", kinds)             or rows(conn, f"SELECT * FROM sources WHERE kind IN ({q}) ORDER BY id DESC LIMIT 1", kinds)
        if r:
            src = _source(r[0])
            lead = rows(conn, "SELECT l.status, l.address, l.verify_note, l.verified_token FROM leads l "
                              "WHERE l.source_id=? ORDER BY l.id DESC LIMIT 1", (r[0]["id"],))
            src["lead"] = lead[0] if lead else None
            if lead and lead[0]["verified_token"]:
                src["lead"]["symbol"] = _symbols(conn, [lead[0]["verified_token"]]).get(lead[0]["verified_token"].lower())
            sources.append(src)

    # --- checking: recent onchain lead checks ----------------------------------
    leads = rows(conn, "SELECT l.id, l.address, l.status, l.verify_note, l.verified_token, l.created_at, "
                       "s.kind AS source_kind, s.confidence AS source_confidence FROM leads l "
                       "JOIN sources s ON s.id=l.source_id WHERE l.status!='unverified' ORDER BY l.id DESC LIMIT 7")
    syms = _symbols(conn, [x["verified_token"] for x in leads if x["verified_token"]])
    for x in leads:
        x["symbol"] = syms.get((x["verified_token"] or "").lower())

    # --- thinking: Muse's latest real decisions (one of each kind if possible) ---
    raw = rows(conn, "SELECT d.*, l.symbol, l.last_progress FROM decisions d LEFT JOIN launches l ON l.token=d.token "
                     "WHERE d.model!='rule' ORDER BY d.id DESC LIMIT 60")
    picked: list[dict[str, Any]] = []
    for want in ("buy", "watch", "pass"):
        m = next((d for d in raw if d["action"] == want and d not in picked), None)
        if m:
            picked.append(m)
    for d in raw:
        if len(picked) >= 3:
            break
        if d not in picked and d["token"] not in {p["token"] for p in picked}:
            picked.append(d)
    gates_by_id = {}
    for j in rows(conn, "SELECT payload FROM journal WHERE kind='decision' ORDER BY id DESC LIMIT 200"):
        p = _j(j["payload"], {})
        gates_by_id[p.get("decision_id")] = p.get("gates") or []
    decisions = []
    for d in sorted(picked, key=lambda d: d["id"], reverse=True):
        ids = [i for i in _j(d["source_ids"], []) if isinstance(i, int)]
        ev = rows(conn, f"SELECT * FROM sources WHERE id IN ({','.join('?' * len(ids))})", ids) if ids else []
        gates = gates_by_id.get(d["id"], [])
        reasons = [r for r in _j(d["reasons"], []) if r not in gates]
        decisions.append({"id": d["id"], "ts": d["ts"], "token": d["token"], "symbol": d["symbol"], "stage": d["stage"],
                          "progress": d["last_progress"], "action": d["action"], "confidence": d["confidence"],
                          "thesis": d["thesis"], "reasons": reasons[:3], "risks": _j(d["risks"], [])[:2],
                          "gates": gates, "evidence": [_source(e) for e in ev][:3], "model": d["model"]})

    # --- live: watching, holding, posting next ---------------------------------
    cands = (kv_get(conn, "candidates") or [])[:6]
    open_pos = rows(conn, "SELECT p.*, l.symbol FROM positions p LEFT JOIN launches l ON l.token=p.token "
                          "WHERE p.status='open' AND p.mode=? ORDER BY p.id", (s.trading_mode.value,))
    held = {p["token"].lower() for p in open_pos}
    watching = []
    for c in cands:
        last = conn.execute("SELECT action FROM decisions WHERE token=? ORDER BY id DESC LIMIT 1", (c["token"],)).fetchone()
        status = "bought" if c["token"].lower() in held else \
            {"watch": "watching", "pass": "passed", "buy": "bought"}.get(last["action"] if last else "", "screening")
        watching.append({"symbol": c.get("symbol"), "token": c["token"], "stage": c.get("stage"),
                         "progress": 1.0 if c.get("stage") == "migrated" else c.get("progress"), "status": status})
    positions = []
    for p in open_pos:
        marks = kv_get(conn, f"marks:{p['id']}") or []
        last = marks[-1] if marks else p["entry_quote"]
        positions.append({"id": p["id"], "symbol": p["symbol"], "token": p["token"], "entry_eth": p["entry_quote"],
                          "opened_at": p["opened_at"], "marks": [p["entry_quote"]] + marks,
                          "pnl_pct": round((last / p["entry_quote"] - 1) * 100, 2) if p["entry_quote"] else 0})
    pending = rows(conn, "SELECT kind, body, created_at FROM posts WHERE status='pending' ORDER BY id LIMIT 1")
    posts = rows(conn, "SELECT kind, body, sent_at, x_post_id FROM posts WHERE status='sent' ORDER BY id DESC LIMIT 6")
    for j in rows(conn, "SELECT ts, payload FROM journal WHERE kind='post_sent' AND payload LIKE '%\"intro\"%'"):
        p = _j(j["payload"], {})  # the intro post was sent by hand, outside the outbox
        if p.get("kind") == "intro":
            posts.append({"kind": "intro", "body": p.get("text", ""), "sent_at": j["ts"], "x_post_id": p.get("post_id")})
    posts = sorted(posts, key=lambda p: p["sent_at"] or "", reverse=True)[:6]

    # --- scorecard ---------------------------------------------------------------
    closed = rows(conn, "SELECT p.pnl_quote, p.closed_at, l.symbol FROM positions p LEFT JOIN launches l "
                        "ON l.token=p.token WHERE p.status='closed' AND p.mode=? ORDER BY p.closed_at",
                        (s.trading_mode.value,))
    cum, series = 0.0, [0.0]
    for c in closed:
        cum += c["pnl_quote"] or 0
        series.append(round(cum, 6))
    wins = sum(1 for c in closed if (c["pnl_quote"] or 0) > 0)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": s.trading_mode.value,
        "kill_switch": {"on": killed, "reason": why},
        "chain": {"block": head, "synced_at": beat.get("at"), "journal_intact": _journal_ok(conn)},
        "funnel": funnel, "stats": stats,
        "sources": sources, "leads": leads, "decisions": decisions,
        "watching": watching, "positions": positions, "slots": s.max_open_positions,
        "next_post": pending[0] if pending else None,
        "posts_24h": one("SELECT COUNT(*) FROM posts WHERE status='sent' AND sent_at>=?", (day,)),
        "posts": posts,
        "score": {"closed": len(closed), "wins": wins, "losses": len(closed) - wins, "net_eth": round(cum, 6),
                  "series": series},
    }
