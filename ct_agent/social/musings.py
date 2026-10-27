"""Original crypto musings on X, one every x_musing_every_min, on top of the trade posts and replies.

Grok writes them: short, lowercase, trader-philosophy lines (the voice is in MUSING_SYSTEM). Each one passes the
same hard filter as replies (no links, addresses, tags or bad words), must not repeat a recent one, and is only
queued while there's room under the daily post cap, so trade posts are never crowded out.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from ..db import journal_append, kv_get, kv_set, rows
from .compose import Draft
from .outbox import enqueue, sent_last_24h
from .replies import clean

log = logging.getLogger(__name__)
RESERVED_FOR_TRADES = 30  # posts per day a musing may never take


def musings_once(ctx: Any) -> dict[str, Any]:
    """Musings on their own timer; with the cadence on (x_cadence_min), they come in its rotation instead."""
    s, conn = ctx.settings, ctx.conn
    if not (s.x_musings_enabled and s.x_posting_enabled and ctx.x.can_post) or s.x_cadence_min > 0:
        return {}
    now = datetime.now(timezone.utc)
    last = kv_get(conn, "musing_last_at")
    if last and datetime.fromisoformat(last) > now - timedelta(minutes=s.x_musing_every_min):
        return {}
    if conn.execute("SELECT 1 FROM posts WHERE kind='musing' AND status='pending' LIMIT 1").fetchone():
        return {}  # the last one hasn't gone out yet
    if sent_last_24h(conn) >= s.x_daily_post_cap - RESERVED_FOR_TRADES:
        return {"musing": "held: leaving room for trade posts"}
    return {"musing": write_musing(ctx, now)}


def write_musing(ctx: Any, now: datetime) -> str:
    """Ask Grok for one musing and queue it. Returns 'queued', 'duplicate' or 'dropped'."""
    s, conn = ctx.settings, ctx.conn
    kv_set(conn, "musing_last_at", now.isoformat(timespec="seconds"))
    recent = [r["body"] for r in rows(conn, "SELECT body FROM posts WHERE kind='musing' ORDER BY id DESC LIMIT 80")]
    day = rows(conn, "SELECT kind, body FROM posts WHERE kind IN ('trade_entry','trade_exit','take_profit','watch') "
                     "AND created_at>=? ORDER BY id DESC LIMIT 5", ((now - timedelta(hours=24)).isoformat(timespec="seconds"),))
    res = ctx.brain.musing({"now_utc": now.strftime("%A %d %B %Y, %H:%M UTC"), "mode": s.trading_mode.value,
                            "my_recent_musings_do_not_repeat": recent,
                            "my_last_24h": [f"{d['kind']}: {d['body'][:140]}" for d in day]})
    d = res.decision
    text = clean(d.tweet) if d else None
    if text and text.lower() in {r.lower() for r in recent}:
        text = None
    if not text:
        journal_append(conn, "musing_dropped", {"draft": (d.tweet if d else "")[:300], "error": res.error})
        return "dropped"
    _, new = enqueue(conn, Draft("musing", text, "", f"musing:{now.strftime('%Y%m%d%H%M')}"))
    return "queued" if new else "duplicate"
