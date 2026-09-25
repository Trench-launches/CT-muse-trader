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


def _hours(a: str | None, b: str | None) -> float | None:
    try:
        return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 3600  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _score(closed: list[dict[str, Any]], open_pos: list[dict[str, Any]], net: float, wins: int,
           series: list[float]) -> dict[str, Any]:
    """The paper scorecard: every closed trade counts, losses included."""
    trades = []
    for c in closed:
        pnl, entry = c["pnl_quote"] or 0.0, c["entry_quote"] or 0.0
        trades.append({"symbol": c["symbol"], "token": c["token"], "pnl_eth": round(pnl, 6),
                       "pnl_pct": round(pnl / entry * 100, 2) if entry else 0.0,
                       "hold_h": _hours(c["opened_at"], c["closed_at"]), "closed_at": c["closed_at"],
                       "reason": c["exit_reason"]})
    deployed = sum(c["entry_quote"] or 0 for c in closed)
    holds = [t["hold_h"] for t in trades if t["hold_h"] is not None]
    unreal = sum(p["marks"][-1] - p["entry_eth"] for p in open_pos if p["marks"])
    by_pnl = sorted(trades, key=lambda t: t["pnl_eth"])
    return {"closed": len(closed), "wins": wins, "losses": len(closed) - wins,
            "taken": len(closed) + len(open_pos), "open": len(open_pos),
            "net_eth": round(net, 6), "deployed_eth": round(deployed, 6),
            "return_pct": round(net / deployed * 100, 2) if deployed else 0.0,
            "win_rate": round(wins / len(closed) * 100, 1) if closed else None,
            "avg_eth": round(net / len(closed), 6) if closed else None,
            "avg_hold_h": round(sum(holds) / len(holds), 2) if holds else None,
            "unrealized_eth": round(unreal, 6),
            "best": by_pnl[-1] if by_pnl else None, "worst": by_pnl[0] if by_pnl else None,
            "recent": trades[::-1][:5], "labels": [t["symbol"] for t in trades],
            "series": series}


def _scanner_watch(conn: Any, s: Settings) -> list[dict[str, Any]]:
    """The latest smart-money calls: live ones first (how far each has run toward the buy trigger), then the
    last few that were bought, didn't move in time, or couldn't be traded."""
    order = "CASE status WHEN 'watching' THEN 0 WHEN 'bought' THEN 1 ELSE 2 END, id DESC"
    calls = rows(conn, "SELECT id, token, symbol, called_at, status, note, last_move_pct, pool_id FROM scanner_calls "
                       f"WHERE status!='watching' OR pool_id IS NOT NULL ORDER BY {order} LIMIT 6")
    now = datetime.now(timezone.utc)
    out = []
    for c in calls:
        move = c["last_move_pct"]
        try:
            left = s.scanner_window_hours - (now - datetime.fromisoformat(c["called_at"])).total_seconds() / 3600
        except (TypeError, ValueError):
            left = None
        status = {"watching": "watching", "bought": "bought", "expired": "expired", "skipped": "skipped"}.get(c["status"], c["status"])
        out.append({"symbol": (c["symbol"] or "?").replace("\U0001f331", "").strip(), "token": c["token"],
                    "status": status, "note": c["note"], "move_pct": move, "called_at": c["called_at"],
                    "left_h": round(left, 2) if left is not None and c["status"] == "watching" else None,
                    "progress": max(0.0, min(1.0, (move or 0) / s.scanner_trigger_pct)) if s.scanner_trigger_pct else 0})
    return out


def _with_v1(sc: dict[str, Any], v1: dict[str, Any]) -> dict[str, Any]:
    """Fold MOOSE v1's paper-trading totals into the scorecard. v1's trades aren't in this journal, so the
    result says how much of it is v1 ("history") and figures v1 didn't give stay the current bot's own."""
    n = int(v1.get("trades") or 0)
    if not n:
        return sc
    wins = int(v1.get("wins") or 0)
    losses = int(v1.get("losses") if v1.get("losses") is not None else n - wins)
    sc = dict(sc)
    sc["closed"] += n
    sc["taken"] += n
    sc["wins"] += wins
    sc["losses"] += losses
    sc["win_rate"] = round(sc["wins"] / sc["closed"] * 100, 1) if sc["closed"] else None
    if v1.get("net_eth") is not None:
        v1_net = float(v1["net_eth"])
        sc["net_eth"] = round(sc["net_eth"] + v1_net, 6)
        sc["avg_eth"] = round(sc["net_eth"] / sc["closed"], 6)
        # the chart starts with v1 as one step, then this bot's trades on top of it
        sc["series"] = [0.0] + [round(v + v1_net, 6) for v in sc["series"]]
        sc["labels"] = [f"MOOSE v1 ({n} trades)"] + list(sc["labels"])
    if v1.get("deployed_eth") is not None:
        sc["deployed_eth"] = round(sc["deployed_eth"] + float(v1["deployed_eth"]), 6)
    if sc["deployed_eth"]:
        sc["return_pct"] = round(sc["net_eth"] / sc["deployed_eth"] * 100, 2)
    if v1.get("avg_hold_h") is not None:
        mine = sc["closed"] - n
        sc["avg_hold_h"] = round((float(v1["avg_hold_h"]) * n + (sc["avg_hold_h"] or 0) * mine) / sc["closed"], 2)
    if v1.get("best_eth") is not None and (not sc["best"] or float(v1["best_eth"]) > sc["best"]["pnl_eth"]):
        sc["best"] = {"symbol": v1.get("best_symbol"), "pnl_eth": float(v1["best_eth"]), "pnl_pct": v1.get("best_pct"),
                      "v1": True}
    if v1.get("worst_eth") is not None and (not sc["worst"] or float(v1["worst_eth"]) < sc["worst"]["pnl_eth"]):
        sc["worst"] = {"symbol": v1.get("worst_symbol"), "pnl_eth": float(v1["worst_eth"]), "pnl_pct": v1.get("worst_pct"),
                       "v1": True}
    sc["history"] = {"v1_trades": n, "v1_wins": wins, "v1_losses": losses,
                     "includes": [k for k in ("net_eth", "deployed_eth", "avg_hold_h", "best_eth", "worst_eth") if v1.get(k) is not None]}
    return sc


def _recent_launches(conn: Any, n: int = 8) -> list[dict[str, Any]]:
    """Coins the launchpad launched, newest first: who made it, and the proof (MOOSE's announcement, the tx)."""
    out = []
    for r in rows(conn, "SELECT r.id, r.symbol, r.name, r.handle, r.token, r.logo, r.tx_hash, r.launched_at, r.status, "
                        "p.x_post_id AS announce_x_id FROM launch_requests r LEFT JOIN posts p "
                        "ON p.idempotency_key='launch_announce:' || r.id AND p.status='sent' "
                        "WHERE r.status IN ('launched','handed_over') ORDER BY r.launched_at DESC LIMIT ?", (n,)):
        out.append({k: r[k] for k in ("symbol", "name", "handle", "token", "logo", "tx_hash", "launched_at",
                                      "announce_x_id")} | {"fees_handed_over": r["status"] == "handed_over"})
    return out


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
                     "WHERE (d.model!='rule' OR d.action='buy') ORDER BY d.id DESC LIMIT 60")
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
    if s.candidate_source == "scanner":
        watching = _scanner_watch(conn, s)
        if s.pons_scan_enabled:  # coins the Pons scan bought aren't smart-money calls: list them first
            called = {w["token"].lower() for w in watching} | {r["token"].lower() for r in rows(
                conn, "SELECT token FROM scanner_calls WHERE status='bought'")}
            pons = [{"symbol": p["symbol"] or "?", "token": p["token"], "status": "bought", "source": "pons",
                     "note": "Pons scan pick", "move_pct": p["pnl_pct"], "called_at": p["opened_at"], "left_h": None,
                     "progress": max(0.0, min(1.0, p["pnl_pct"] / s.scanner_trigger_pct)) if s.scanner_trigger_pct else 0}
                    for p in reversed(positions) if p["token"].lower() not in called]
            watching = (pons + watching)[:6]
        live = {p["token"].lower(): p["pnl_pct"] for p in positions}
        for w in watching:  # a coin it holds shows the trade's P&L now, not the call's move frozen at the buy
            if w["status"] == "bought" and w["token"].lower() in live:
                w["move_pct"] = live[w["token"].lower()]
    # MOOSE's own posts only: replies to people (auto-replies, launch answers) stay on X
    own = "kind NOT IN ('reply','launch','launch_failed','launch_handed','launch_followup')"
    pending = rows(conn, f"SELECT kind, body, created_at FROM posts WHERE status='pending' AND {own} ORDER BY id LIMIT 1")
    posts = rows(conn, f"SELECT kind, body, sent_at, x_post_id FROM posts WHERE status='sent' AND {own} ORDER BY id DESC LIMIT 6")
    for j in rows(conn, "SELECT ts, payload FROM journal WHERE kind='post_sent' AND payload LIKE '%\"intro\"%'"):
        p = _j(j["payload"], {})  # the intro post was sent by hand, outside the outbox
        if p.get("kind") == "intro":
            posts.append({"kind": "intro", "body": p.get("text", ""), "sent_at": j["ts"], "x_post_id": p.get("post_id")})
    posts = sorted(posts, key=lambda p: p["sent_at"] or "", reverse=True)[:6]
    # The trade tweets, one thread per coin (the buy, Muse's reasons under it, updates, sells), newest trade first:
    # the public record of every coin it bought, as posted on X.
    trade_posts = rows(conn, "SELECT p.kind, p.body, p.sent_at, p.x_post_id, p.ref, l.symbol FROM posts p "
                             "LEFT JOIN launches l ON lower(l.token)=lower(p.ref) WHERE p.status='sent' AND p.kind IN "
                             "('trade_entry','analysis','update','take_profit','trade_exit') AND p.x_post_id IS NOT NULL "
                             "AND p.sent_at>=? ORDER BY p.sent_at", (s.score_since,))
    threads: dict[str, dict[str, Any]] = {}
    for p in trade_posts:
        if p["kind"] == "trade_entry" or (p["ref"] or "").lower() in threads:
            t = threads.setdefault(p["ref"].lower(), {"token": p["ref"], "symbol": p["symbol"], "posts": []})
            t["posts"].append({k: p[k] for k in ("kind", "body", "sent_at", "x_post_id")})
    trade_threads = sorted(threads.values(), key=lambda t: t["posts"][0]["sent_at"], reverse=True)[:8]

    # --- scorecard ---------------------------------------------------------------
    closed = rows(conn, "SELECT p.token, p.pnl_quote, p.entry_quote, p.opened_at, p.closed_at, p.exit_reason, l.symbol "
                        "FROM positions p LEFT JOIN launches l ON l.token=p.token "
                        "WHERE p.status='closed' AND p.mode=? AND p.opened_at>=? ORDER BY p.closed_at",
                        (s.trading_mode.value, s.score_since))
    cum, series = 0.0, [0.0]
    for c in closed:
        cum += c["pnl_quote"] or 0
        series.append(round(cum, 6))
    wins = sum(1 for c in closed if (c["pnl_quote"] or 0) > 0)
    score = _with_v1(_score(closed, positions, cum, wins, series), s.score_history_v1)

    # Coin logos: only for tokens that store one onchain; the site falls back to a coin if it won't load.
    with_logo = {r["token"].lower() for r in rows(conn, "SELECT token FROM launches WHERE logo_url IS NOT NULL "
                                                        "AND logo_url != ''")}
    for item in watching + positions + decisions + leads + score["recent"]:
        tok = item.get("token") or item.get("verified_token")
        item["logo"] = f"/logo/{tok}" if tok and tok.lower() in with_logo else None

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": s.trading_mode.value,
        "kill_switch": {"on": killed, "reason": why},
        "chain": {"block": head, "synced_at": beat.get("at"), "journal_intact": _journal_ok(conn)},
        "funnel": funnel, "stats": stats,
        "sources": sources, "leads": leads, "decisions": decisions,
        "watching": watching, "positions": positions, "slots": s.max_open_positions,
        "watch_mode": s.candidate_source, "trigger_pct": s.scanner_trigger_pct, "window_h": s.scanner_window_hours,
        "pons_scan": s.pons_scan_enabled, "buy_on_call": s.scanner_buy_on_call,
        "wallet": kv_get(conn, "live_wallet") if s.trading_mode.value == "live" else None,
        "size_pct": s.position_size_pct,
        "launchpad": {"enabled": s.launchpad_enabled and bool(s.launcher_wallet_private_key),
                      "recent": _recent_launches(conn)},
        "next_post": pending[0] if pending else None,
        "posts_24h": one("SELECT COUNT(*) FROM posts WHERE status='sent' AND sent_at>=?", (day,)),
        "posts": posts, "trade_threads": trade_threads,
        "score": score,
    }
