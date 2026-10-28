"""Everything the public website shows, in one read-only payload.

The landing page polls GET /site. Figures are real: counts from the
smart-money scanner, the latest sources and onchain checks, Grok's actual
reasoning, open positions, posts that went out, and the scorecard. Amounts
are in SOL (keys end in _sol).
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
    from ..market.tokens import symbols
    return symbols(conn, tokens)


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
    """The scorecard: every closed trade counts, losses included. Amounts in SOL."""
    trades = []
    for c in closed:
        pnl, entry = c["pnl_quote"] or 0.0, c["entry_quote"] or 0.0
        trades.append({"symbol": c["symbol"], "token": c["token"], "pnl_sol": round(pnl, 6),
                       "pnl_pct": round(pnl / entry * 100, 2) if entry else 0.0,
                       "hold_h": _hours(c["opened_at"], c["closed_at"]), "closed_at": c["closed_at"],
                       "reason": c["exit_reason"]})
    deployed = sum(c["entry_quote"] or 0 for c in closed)
    holds = [t["hold_h"] for t in trades if t["hold_h"] is not None]
    unreal = sum(p["marks"][-1] - p["entry_sol"] for p in open_pos if p["marks"])
    by_pnl = sorted(trades, key=lambda t: t["pnl_sol"])
    return {"closed": len(closed), "wins": wins, "losses": len(closed) - wins,
            "taken": len(closed) + len(open_pos), "open": len(open_pos),
            "net_sol": round(net, 6), "deployed_sol": round(deployed, 6),
            "return_pct": round(net / deployed * 100, 2) if deployed else 0.0,
            "win_rate": round(wins / len(closed) * 100, 1) if closed else None,
            "avg_sol": round(net / len(closed), 6) if closed else None,
            "avg_hold_h": round(sum(holds) / len(holds), 2) if holds else None,
            "unrealized_sol": round(unreal, 6),
            "best": by_pnl[-1] if by_pnl else None, "worst": by_pnl[0] if by_pnl else None,
            "recent": trades[::-1][:5], "labels": [t["symbol"] for t in trades],
            "series": series}


def _scanner_watch(conn: Any, s: Settings) -> list[dict[str, Any]]:
    """The latest smart-money calls: live ones first (how far each has run toward the buy trigger), then the
    last few that were bought, didn't move in time, or couldn't be traded."""
    order = "CASE c.status WHEN 'watching' THEN 0 WHEN 'bought' THEN 1 ELSE 2 END, c.id DESC"
    calls = rows(conn, "SELECT c.id, c.token, c.symbol, c.called_at, c.status, c.note, c.last_move_pct, c.pool_id, "
                       "t.stage, t.dex_id FROM scanner_calls c LEFT JOIN tokens t ON t.mint=c.token "
                       f"WHERE c.status!='watching' OR c.pool_id IS NOT NULL ORDER BY {order} LIMIT 6")
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
                    "stage": c["stage"], "dex": c["dex_id"]})
    return out


V1_KEYS = ("net_sol", "deployed_sol", "avg_hold_h", "best_sol", "worst_sol")


def _with_v1(sc: dict[str, Any], v1: dict[str, Any]) -> dict[str, Any]:
    """Fold an earlier VIGIL's paper-trading totals (CT_SCORE_HISTORY_V1, amounts in SOL) into the scorecard. Those
    trades aren't in this journal, so the result says how much of it is history and figures v1 didn't give stay
    the current bot's own."""
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
    if v1.get("net_sol") is not None:
        v1_net = float(v1["net_sol"])
        sc["net_sol"] = round(sc["net_sol"] + v1_net, 6)
        sc["avg_sol"] = round(sc["net_sol"] / sc["closed"], 6)
        # the chart starts with v1 as one step, then this bot's trades on top of it
        sc["series"] = [0.0] + [round(v + v1_net, 6) for v in sc["series"]]
        sc["labels"] = [f"VIGIL v1 ({n} trades)"] + list(sc["labels"])
    if v1.get("deployed_sol") is not None:
        sc["deployed_sol"] = round(sc["deployed_sol"] + float(v1["deployed_sol"]), 6)
    if sc["deployed_sol"]:
        sc["return_pct"] = round(sc["net_sol"] / sc["deployed_sol"] * 100, 2)
    if v1.get("avg_hold_h") is not None:
        mine = sc["closed"] - n
        sc["avg_hold_h"] = round((float(v1["avg_hold_h"]) * n + (sc["avg_hold_h"] or 0) * mine) / sc["closed"], 2)
    if v1.get("best_sol") is not None and (not sc["best"] or float(v1["best_sol"]) > sc["best"]["pnl_sol"]):
        sc["best"] = {"symbol": v1.get("best_symbol"), "pnl_sol": float(v1["best_sol"]), "pnl_pct": v1.get("best_pct"),
                      "v1": True}
    if v1.get("worst_sol") is not None and (not sc["worst"] or float(v1["worst_sol"]) < sc["worst"]["pnl_sol"]):
        sc["worst"] = {"symbol": v1.get("worst_symbol"), "pnl_sol": float(v1["worst_sol"]),
                       "pnl_pct": v1.get("worst_pct"), "v1": True}
    sc["history"] = {"v1_trades": n, "v1_wins": wins, "v1_losses": losses,
                     "includes": [k for k in V1_KEYS if v1.get(k) is not None]}
    return sc


# Coin images are pinned on Pinata (where the launcher uploads them). The public ipfs.io gateway rate-limits
# browsers (HTTP 429), so the site is given the same file through Pinata's gateway instead. The metadata on
# chain is left as it is.
PUBLIC_IPFS = ("https://ipfs.io/ipfs/", "https://dweb.link/ipfs/", "https://cloudflare-ipfs.com/ipfs/")
SHOW_IPFS = "https://gateway.pinata.cloud/ipfs/"


def shown_image(url: str | None) -> str | None:
    """The URL the site should load a coin image from."""
    for gw in PUBLIC_IPFS:
        if url and url.startswith(gw):
            return SHOW_IPFS + url[len(gw):]
    return url


def recent_launches(conn: Any, n: int = 8) -> list[dict[str, Any]]:
    """Coins the launchpad launched, newest first: who made it, and the proof (VIGIL's announcement, the
    transaction, the creator-fee split set on chain)."""
    out = []
    for r in rows(conn, "SELECT r.id, r.symbol, r.name, r.handle, r.token, COALESCE(r.image_uri, r.logo) AS logo, "
                        "r.tx_hash, r.fee_shares, "
                        "r.launched_at, p.x_post_id AS announce_x_id FROM launch_requests r LEFT JOIN posts p "
                        "ON p.idempotency_key='launch_announce:' || r.id AND p.status='sent' "
                        "WHERE r.status='launched' ORDER BY r.launched_at DESC LIMIT ?", (n,)):
        item = {k: r[k] for k in ("symbol", "name", "handle", "token", "logo", "tx_hash", "launched_at",
                                  "announce_x_id")}
        item["fee_shares"] = _j(r["fee_shares"], None)
        item["logo"] = shown_image(item["logo"])
        out.append(item)
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

    # --- funnel: the scanner's Solana calls in the last 24h ---------------------
    # calls: what the smart-money scanner called; screened: checked onchain and read by Grok; bought: traded.
    funnel = {
        "calls_24h": one("SELECT COUNT(*) FROM scanner_calls WHERE called_at>=?", (day,)),
        "screened_24h": one("SELECT COUNT(*) FROM scanner_calls WHERE called_at>=? AND decision_id IS NOT NULL", (day,)),
        "bought_24h": one("SELECT COUNT(*) FROM scanner_calls WHERE called_at>=? AND status='bought'", (day,)),
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
                src["lead"]["symbol"] = _symbols(conn, [lead[0]["verified_token"]]).get(lead[0]["verified_token"])
            sources.append(src)

    # --- checking: recent onchain lead checks ----------------------------------
    leads = rows(conn, "SELECT l.id, l.address, l.status, l.verify_note, l.verified_token, l.created_at, "
                       "s.kind AS source_kind, s.confidence AS source_confidence FROM leads l "
                       "JOIN sources s ON s.id=l.source_id WHERE l.status!='unverified' ORDER BY l.id DESC LIMIT 7")
    syms = _symbols(conn, [x["verified_token"] for x in leads if x["verified_token"]])
    for x in leads:
        x["symbol"] = syms.get(x["verified_token"] or "")

    # --- thinking: Grok's latest real decisions (one of each kind if possible) ---
    raw = rows(conn, "SELECT d.*, t.symbol, t.stage AS coin_stage, t.dex_id FROM decisions d "
                     "LEFT JOIN tokens t ON t.mint=d.token "
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
        decisions.append({"id": d["id"], "ts": d["ts"], "token": d["token"], "symbol": d["symbol"],
                          "stage": d["coin_stage"], "dex": d["dex_id"], "action": d["action"], "confidence": d["confidence"],
                          "thesis": d["thesis"], "reasons": reasons[:3], "risks": _j(d["risks"], [])[:2],
                          "gates": gates, "evidence": [_source(e) for e in ev][:3], "model": d["model"]})

    # --- live: watching, holding, posting next ---------------------------------
    open_pos = rows(conn, "SELECT p.*, t.symbol, t.stage, t.dex_id FROM positions p LEFT JOIN tokens t ON t.mint=p.token "
                          "WHERE p.status='open' AND p.mode=? ORDER BY p.id", (s.trading_mode.value,))
    positions = []
    for p in open_pos:
        marks = kv_get(conn, f"marks:{p['id']}") or []
        last = marks[-1] if marks else p["entry_quote"]
        positions.append({"id": p["id"], "symbol": p["symbol"], "token": p["token"], "entry_sol": p["entry_quote"],
                          "opened_at": p["opened_at"], "marks": [p["entry_quote"]] + marks,
                          "pnl_pct": round((last / p["entry_quote"] - 1) * 100, 2) if p["entry_quote"] else 0,
                          "stage": p["stage"], "dex": p["dex_id"]})
    watching = _scanner_watch(conn, s)
    live = {p["token"]: p["pnl_pct"] for p in positions}
    for w in watching:  # a coin it holds shows the trade's P&L now, not the call's move frozen at the buy
        if w["status"] == "bought" and w["token"] in live:
            w["move_pct"] = live[w["token"]]
    # VIGIL's own posts only: replies to people (auto-replies, launch answers) stay on X
    own = "kind NOT IN ('reply','launch','launch_failed','launch_handed','launch_followup')"
    pending = rows(conn, f"SELECT kind, body, created_at FROM posts WHERE status='pending' AND {own} ORDER BY id LIMIT 1")
    posts = rows(conn, f"SELECT kind, body, sent_at, x_post_id FROM posts WHERE status='sent' AND {own} ORDER BY id DESC LIMIT 6")
    for j in rows(conn, "SELECT ts, payload FROM journal WHERE kind='post_sent' AND payload LIKE '%\"intro\"%'"):
        p = _j(j["payload"], {})  # the intro post was sent by hand, outside the outbox
        if p.get("kind") == "intro":
            posts.append({"kind": "intro", "body": p.get("text", ""), "sent_at": j["ts"], "x_post_id": p.get("post_id")})
    posts = sorted(posts, key=lambda p: p["sent_at"] or "", reverse=True)[:6]
    # The trade tweets, one thread per coin (the buy, Grok's reasons under it, updates, sells), newest trade first:
    # the public record of every coin it bought, as posted on X.
    trade_posts = rows(conn, "SELECT p.kind, p.body, p.sent_at, p.x_post_id, p.ref, t.symbol FROM posts p "
                             "LEFT JOIN tokens t ON t.mint=p.ref WHERE p.status='sent' AND p.kind IN "
                             "('trade_entry','analysis','update','take_profit','trade_exit') AND p.x_post_id IS NOT NULL "
                             "AND p.sent_at>=? ORDER BY p.sent_at", (s.score_since,))
    threads: dict[str, dict[str, Any]] = {}
    for p in trade_posts:
        if p["kind"] == "trade_entry" or (p["ref"] or "") in threads:
            t = threads.setdefault(p["ref"], {"token": p["ref"], "symbol": p["symbol"], "posts": []})
            t["posts"].append({k: p[k] for k in ("kind", "body", "sent_at", "x_post_id")})
    trade_threads = sorted(threads.values(), key=lambda t: t["posts"][0]["sent_at"], reverse=True)[:8]

    # --- scorecard ---------------------------------------------------------------
    closed = rows(conn, "SELECT p.token, p.pnl_quote, p.entry_quote, p.opened_at, p.closed_at, p.exit_reason, t.symbol "
                        "FROM positions p LEFT JOIN tokens t ON t.mint=p.token "
                        "WHERE p.status='closed' AND p.mode=? AND p.opened_at>=? ORDER BY p.closed_at",
                        (s.trading_mode.value, s.score_since))
    cum, series = 0.0, [0.0]
    for c in closed:
        cum += c["pnl_quote"] or 0
        series.append(round(cum, 6))
    wins = sum(1 for c in closed if (c["pnl_quote"] or 0) > 0)
    score = _with_v1(_score(closed, positions, cum, wins, series), s.score_history_v1)

    # Coin logos: only for coins whose metadata has one; the site falls back to a coin if it won't load.
    items = watching + positions + decisions + leads + score["recent"]
    want = [i.get("token") or i.get("verified_token") for i in items]
    want = [t for t in dict.fromkeys(want) if t]
    with_logo = {r["mint"] for r in rows(conn, f"SELECT mint FROM tokens WHERE mint IN ({','.join('?' * len(want))}) "
                                               "AND logo_url IS NOT NULL AND logo_url != ''", want)} if want else set()
    for item in items:
        tok = item.get("token") or item.get("verified_token")
        item["logo"] = f"/logo/{tok}" if tok and tok in with_logo else None

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": s.trading_mode.value,
        "kill_switch": {"on": killed, "reason": why},
        "chain": {"name": "solana", "explorer": s.explorer_url, "journal_intact": _journal_ok(conn)},
        "funnel": funnel, "stats": stats,
        "sources": sources, "leads": leads, "decisions": decisions,
        "watching": watching, "positions": positions, "slots": s.max_open_positions,
        "trigger_pct": s.scanner_trigger_pct, "window_h": s.scanner_window_hours, "buy_on_call": s.scanner_buy_on_call,
        "wallet": kv_get(conn, "live_wallet") if s.trading_mode.value == "live" else None,
        "size_pct": s.position_size_pct,
        "launchpad": {"enabled": s.launchpad_enabled and bool(s.launcher_wallet_private_key),
                      "recent": recent_launches(conn)},
        "next_post": pending[0] if pending else None,
        "posts_24h": one("SELECT COUNT(*) FROM posts WHERE status='sent' AND sent_at>=?", (day,)),
        "posts": posts, "trade_threads": trade_threads,
        "score": score,
    }


# --- the launchpad dashboard (/launchpad on the site) -------------------------------------------------------------

# pump.fun's standard curve starts with 793.1M tokens for sale (6 decimals); a coin graduates when they're all sold
CURVE_START_TOKENS = 793_100_000 * 10**6
LAMPORTS = 10**9
_BOARD: dict[str, Any] = {}
BOARD_CACHE_S = 60


def curve_progress(data: bytes | None) -> dict[str, Any] | None:
    """How far a pump.fun bonding curve is to graduating, from its account data: BondingCurve is 8 bytes of
    discriminator, then virtual token, virtual SOL, real token and real SOL reserves (u64 each), supply, complete.
    Only for standard SOL curves (the kind VIGIL launches): pump.fun also runs curves quoted in other coins or
    starting with more tokens, and for those this formula would be wrong, so they get no progress (None)."""
    if not data or len(data) < 49:
        return None
    real_tokens, real_sol = (int.from_bytes(data[o:o + 8], "little") for o in (24, 32))
    done = data[48] == 1
    try:
        from ..launchpad.pumpfun import WSOL_MINT, parse_bonding_curve
        quote = parse_bonding_curve(data)["quote_mint"]
    except Exception:
        quote = None
    if quote not in (None, str(WSOL_MINT)) or (not done and real_tokens > CURVE_START_TOKENS):
        return None
    pct = 100.0 if done else max(0.0, min(100.0, (1 - real_tokens / CURVE_START_TOKENS) * 100))
    return {"progress_pct": round(pct, 1), "graduated": done, "curve_sol": round(real_sol / LAMPORTS, 3)}


def _requester_bps(fee_shares: Any, recipient: str | None) -> int:
    for sh in fee_shares or []:
        if recipient and sh.get("address") == recipient:
            return int(sh.get("bps") or 0)
    return 0


def launch_board(conn: Any, s: Settings, curves: Any = None, prices_usd: Any = None) -> dict[str, Any]:
    """Everything the launchpad dashboard shows: launch stats, every launched coin with its curve progress and
    price, and the latest creator-fee payouts. curves([mints]) -> {mint: curve account bytes} and
    prices_usd([mints]) -> {mint: usd} are passed in (one chain read and one market read for all coins), so a
    missing or failing one just leaves those fields empty."""
    coins = [dict(r) for r in rows(conn, "SELECT symbol, name, handle, token, COALESCE(image_uri, logo) AS logo, "
                                         "tx_hash, fee_shares, recipient, launched_at FROM launch_requests "
                                         "WHERE status='launched' "
                                         "ORDER BY launched_at DESC LIMIT 200")]
    mints = [c["token"] for c in coins]
    prices, curve_bytes = _try(prices_usd, mints), _try(curves, mints)
    by_mint = {}
    for c in coins:
        shares = _j(c.pop("fee_shares"), None)
        c["creator_share_bps"] = _requester_bps(shares, c.pop("recipient"))
        c["logo"] = shown_image(c["logo"])
        c["fee_split_locked"] = shares is not None
        c.update(curve_progress(curve_bytes.get(c["token"])) or {"progress_pct": None, "graduated": None, "curve_sol": None})
        px = prices.get(c["token"])
        c["price_usd"] = px
        c["market_cap_usd"] = round(px * 1_000_000_000, 2) if px else None  # pump.fun coins have 1B supply
        by_mint[c["token"]] = c
    payouts = []
    for j in rows(conn, "SELECT ts, payload FROM journal WHERE kind='launchpad_distributed' ORDER BY id DESC LIMIT 12"):
        p = _j(j["payload"], {})
        c = by_mint.get(p.get("mint"), {})
        total = int(p.get("lamports") or 0)
        payouts.append({"at": j["ts"], "token": p.get("mint"), "symbol": c.get("symbol"), "signature": p.get("signature"),
                        "total_sol": round(total / LAMPORTS, 6),
                        "creator_sol": round(total * (c.get("creator_share_bps") or 0) / 10_000 / LAMPORTS, 6)})
    paid = sum(p["creator_sol"] for p in payouts)
    day = _since(24)
    n = lambda q, *a: conn.execute(q, a).fetchone()[0]  # noqa: E731
    stats = {
        "enabled": s.launchpad_enabled, "creator_share_bps": s.launch_creator_share_bps,
        "launched": len(coins), "launched_24h": sum(1 for c in coins if (c["launched_at"] or "") >= day),
        "requests_24h": n("SELECT COUNT(*) FROM launch_requests WHERE created_at>=?", day),
        "refused_24h": n("SELECT COUNT(*) FROM launch_requests WHERE status='refused' AND created_at>=?", day),
        "per_user_day": s.launch_per_user_day, "min_account_days": s.launch_min_account_days,
        "creator_fees_paid_sol": round(paid, 6),
    }
    return {"stats": stats, "coins": coins, "payouts": payouts, "generated_at": datetime.now(timezone.utc).isoformat()}


def _try(read: Any, mints: list[str]) -> dict[str, Any]:
    """A chain or market read for the board: decoration, so a failure leaves the fields empty instead of failing."""
    if not read or not mints:
        return {}
    try:
        return read(mints) or {}
    except Exception:
        return {}


def launch_board_cached(conn: Any, s: Settings, curves: Any = None, prices_usd: Any = None) -> dict[str, Any]:
    hit = _BOARD.get("v")
    if hit and time.time() - hit[0] < BOARD_CACHE_S:
        return hit[1]
    out = launch_board(conn, s, curves, prices_usd)
    _BOARD["v"] = (time.time(), out)
    return out
