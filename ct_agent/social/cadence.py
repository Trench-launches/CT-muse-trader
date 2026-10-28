"""A steady timeline: a post at least every x_cadence_min minutes, in a rotation of kinds.

Trades, updates and launches still post the moment they happen, and each one counts as that slot's post. Only
when x_cadence_min minutes pass with nothing new on the timeline does VIGIL fill the gap with the next kind in
the rotation that has something to say:

  scan     what the scanner looked at (smart-money calls today, coins Grok read this hour, what was bought)
  holdings the open bags and how each one is doing
  musing   a short line from Grok (musings.py)
  score    the running scorecard, losses included
  goals    what VIGIL is aiming for next and what its data has taught it (goals.py), every few hours

So every hour carries a mix of kinds. Replies to people and thread replies never count toward the timeline.
The facts come from the same numbers the website shows. Each post names at most one cashtag (X refuses more).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from ..db import kv_get, kv_set, rows
from .compose import Draft
from .outbox import QUOTE_KINDS, enqueue, sent_last_24h

log = logging.getLogger(__name__)
ROTATION = ("scan", "holdings", "musing", "score", "goals")
FILLER_KINDS = {"scan_report", "holdings", "musing", "scorecard_hourly", "goals"}
RESERVED_FOR_TRADES = 30  # posts per day a filler may never take


def _on_timeline(r: dict[str, Any]) -> bool:
    """Seen on the profile: a post of its own, or a follow-up quote post (not a reply to someone, not a thread)."""
    key = r.get("reply_to_key") or ""
    return not key or (r["kind"] in QUOTE_KINDS and not key.startswith("x:"))


def last_timeline_at(conn: Any) -> datetime | None:
    for r in rows(conn, "SELECT kind, reply_to_key, sent_at FROM posts WHERE status='sent' AND sent_at IS NOT NULL "
                        "ORDER BY sent_at DESC LIMIT 50"):
        if _on_timeline(r):
            return datetime.fromisoformat(r["sent_at"])
    return None


def _timeline_waiting(conn: Any) -> bool:
    return any(_on_timeline(r) for r in rows(conn, "SELECT kind, reply_to_key FROM posts WHERE status IN "
                                                   "('pending','sending')"))


def _pct(x: float) -> str:
    return f"{'+' if x >= 0 else ''}{x:.0f}%"


def scan_post(conn: Any, site: dict[str, Any], now: datetime) -> str | None:
    hour = (now - timedelta(hours=1)).isoformat(timespec="seconds")
    acts = {r["action"]: r["n"] for r in rows(conn, "SELECT action, COUNT(*) n FROM decisions WHERE ts>=? GROUP BY action",
                                              (hour,))}
    looked = sum(acts.values())
    f = site.get("funnel") or {}
    if not looked and not f.get("calls_24h"):
        return None
    pick = conn.execute("SELECT t.symbol FROM decisions d LEFT JOIN tokens t ON t.mint=d.token WHERE d.ts>=? AND "
                        "d.action IN ('buy','watch') AND t.symbol IS NOT NULL ORDER BY d.confidence DESC LIMIT 1",
                        (hour,)).fetchone()
    lines = ["🔎 Scan report, last hour", "",
             f"Smart-money calls on Solana today: {f.get('calls_24h', 0):,}",
             f"Screened by Grok this hour: {looked}"]
    if looked:
        lines.append(f"Bought: {acts.get('buy', 0)} · Watching: {acts.get('watch', 0)} · Passed: {acts.get('pass', 0)}")
    if pick:
        lines += ["", f"Best look right now: ${pick['symbol']}"]
    else:
        lines += ["", "Nothing worth a buy yet. Patience is a position."]
    return "\n".join(lines)


def holdings_post(site: dict[str, Any]) -> str | None:
    pos = sorted(site.get("positions") or [], key=lambda p: -(p.get("pnl_pct") or 0))
    if not pos:
        return None
    sc = site.get("score") or {}
    lines = ["📊 My open bags", ""] + [f"{p['symbol']} {_pct(p.get('pnl_pct') or 0)}" for p in pos[:6]]
    if len(pos) > 6:
        lines.append(f"+{len(pos) - 6} more")
    u = sc.get("unrealized_sol")
    lines += ["", f"{len(pos)} open" + (f" · {u:+.4f} SOL unrealised" if u is not None else ""),
              "Stops and targets are live on every one."]
    return "\n".join(lines)


def score_post(site: dict[str, Any]) -> str | None:
    sc = site.get("score") or {}
    if not sc.get("taken"):
        return None
    # The site sends null (not 0) for numbers that don't exist yet, such as a win rate before any trade closes.
    closed, win = sc.get("closed") or 0, sc.get("win_rate")
    lines = ["📈 VIGIL scorecard", "",
             f"Trades: {sc['taken']} ({closed} closed, {sc.get('open') or 0} open)",
             f"Win rate: {win:.0f}%" if closed and win is not None else "Win rate: none closed yet",
             f"Closed P&L: {sc.get('net_sol') or 0:+.4f} SOL"]
    best, worst = sc.get("best") or {}, sc.get("worst") or {}
    if best.get("symbol") and worst.get("symbol"):
        lines.append(f"Best: {best['symbol']} {_pct(best.get('pnl_pct') or 0)} · Worst: {worst['symbol']} "
                     f"{_pct(worst.get('pnl_pct') or 0)}")
    lines += ["", "Every trade posted, losses included."]
    return "\n".join(lines)


def cadence_once(ctx: Any, site: dict[str, Any] | None = None, now: datetime | None = None) -> dict[str, Any]:
    s, conn = ctx.settings, ctx.conn
    if not (s.x_posting_enabled and s.x_cadence_min > 0 and ctx.x.can_post):
        return {}
    now = now or datetime.now(timezone.utc)
    last = last_timeline_at(conn)
    if last and now - last < timedelta(minutes=s.x_cadence_min):
        return {}
    if _timeline_waiting(conn):
        return {}  # something is already on its way out: that's this slot's post
    if sent_last_24h(conn) >= s.x_daily_post_cap - RESERVED_FOR_TRADES:
        return {"cadence": "held: leaving room for trade posts"}
    if site is None:
        from ..api.site import site_data
        site = site_data(conn, s)
    start = int(kv_get(conn, "cadence_next") or 0)
    for i in range(len(ROTATION)):
        kind = ROTATION[(start + i) % len(ROTATION)]
        if kind == "musing":
            if not s.x_musings_enabled:
                continue
            from .musings import write_musing
            got = write_musing(ctx, now)
            if got != "queued":
                continue
            kv_set(conn, "cadence_next", (start + i + 1) % len(ROTATION))
            return {"cadence": "musing"}
        if kind == "goals":
            if not s.x_goals_enabled:
                continue
            from .goals import write_goals
            try:
                got = write_goals(ctx, site, now)
            except Exception as e:
                log.warning("cadence goals post failed: %s", e)
                continue
            if got != "queued":
                continue  # not due yet (one every x_goals_every_hours), or nothing good to say
            kv_set(conn, "cadence_next", (start + i + 1) % len(ROTATION))
            return {"cadence": "goals"}
        try:
            body = scan_post(conn, site, now) if kind == "scan" else holdings_post(site) if kind == "holdings" \
                else score_post(site)
        except Exception as e:  # one broken kind must not stall the rotation: the next kind still posts
            log.warning("cadence %s post failed: %s", kind, e)
            continue
        pkind = {"scan": "scan_report", "holdings": "holdings", "score": "scorecard_hourly"}[kind]
        if not body:
            continue
        prev = conn.execute("SELECT body FROM posts WHERE kind=? ORDER BY id DESC LIMIT 1", (pkind,)).fetchone()
        if prev and prev["body"] == body:
            continue  # nothing changed since the last one (X also refuses exact repeats)
        enqueue(conn, Draft(pkind, body, "", f"{pkind}:{now.strftime('%Y%m%d%H%M')}"))
        kv_set(conn, "cadence_next", (start + i + 1) % len(ROTATION))
        return {"cadence": kind}
    return {"cadence": "nothing to say"}
