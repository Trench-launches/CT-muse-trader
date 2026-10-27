"""A steady timeline: a post at least every x_cadence_min minutes, in a rotation of kinds.

Trades, updates and launches still post the moment they happen, and each one counts as that slot's post. Only
when x_cadence_min minutes pass with nothing new on the timeline does HORIZON fill the gap with the next kind in
the rotation that has something to say:

  scan     what the scanner looked at in the last hour (launches seen, coins screened, bought/watched/passed)
  holdings the open bags and how each one is doing
  musing   a short line from Grok (musings.py)
  score    the running scorecard, losses included

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
ROTATION = ("scan", "holdings", "musing", "score")
FILLER_KINDS = {"scan_report", "holdings", "musing", "scorecard_hourly"}
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
    if not looked and not f.get("launched_24h"):
        return None
    pick = conn.execute("SELECT l.symbol FROM decisions d LEFT JOIN launches l ON l.token=d.token WHERE d.ts>=? AND "
                        "d.action IN ('buy','watch') AND l.symbol IS NOT NULL ORDER BY d.confidence DESC LIMIT 1",
                        (hour,)).fetchone()
    lines = ["🔎 Scan report, last hour", "",
             f"New coins on Robinhood Chain today: {f.get('launched_24h', 0):,}",
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
    u = sc.get("unrealized_eth")
    lines += ["", f"{len(pos)} open" + (f" · {u:+.4f} ETH unrealised" if u is not None else ""),
              "Stops and targets are live on every one."]
    return "\n".join(lines)


def score_post(site: dict[str, Any]) -> str | None:
    sc = site.get("score") or {}
    if not sc.get("taken"):
        return None
    lines = ["📈 HORIZON scorecard", "",
             f"Trades: {sc['taken']} ({sc.get('closed', 0)} closed, {sc.get('open', 0)} open)",
             f"Win rate: {sc.get('win_rate', 0):.0f}%",
             f"Closed P&L: {sc.get('net_eth', 0):+.4f} ETH"]
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
        body = scan_post(conn, site, now) if kind == "scan" else holdings_post(site) if kind == "holdings" \
            else score_post(site)
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
