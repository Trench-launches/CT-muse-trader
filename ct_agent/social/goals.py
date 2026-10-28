"""Goal posts: every few hours VIGIL says what it's aiming for next and what its own data has taught it so far.

Grok writes them (GOALS_SYSTEM), but every number comes from facts computed here from the trade log, the scanner and
the launchpad, so a goal post never claims anything that didn't happen. Aims are about getting better (more data,
sharper filters, cleaner exits, more launches), never a profit or price target. They come in the cadence rotation,
at most one every x_goals_every_hours, and pass the same hard filter as replies and musings.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from ..db import journal_append, rows
from .compose import Draft
from .outbox import enqueue
from .replies import clean

HEADER = "🎯 Where I'm headed\n\n"
MAX_TEXT = 240  # with the header, inside X's 280
STEPS = {"closed trades": (1, 10, 25, 50, 100, 250, 500, 1000),
         "smart-money calls read": (50, 100, 250, 500, 1000, 2500, 5000, 10000),
         "take-profits hit": (10, 25, 50, 100, 250, 500),
         "coins launched through the launchpad": (10, 25, 50, 100, 250, 500)}
CASHTAG = re.compile(r"\$[A-Za-z]")


def _next(n: int, steps: tuple[int, ...]) -> int | None:
    return next((x for x in steps if x > n), None)


def goal_facts(conn: Any, site: dict[str, Any], mode: str, now: datetime) -> dict[str, Any]:
    """What VIGIL has done so far, and the next round number for each count."""
    sc = site.get("score") or {}
    calls = {r["status"]: r["n"] for r in rows(conn, "SELECT status, COUNT(*) n FROM scanner_calls GROUP BY status")}
    taxed = conn.execute("SELECT COUNT(*) FROM scanner_calls WHERE status='skipped' AND note LIKE '%tax%'").fetchone()[0]
    first = conn.execute("SELECT MIN(opened_at) FROM positions WHERE mode=?", (mode,)).fetchone()[0]
    tps = conn.execute("SELECT COALESCE(SUM(tp_hits), 0) FROM positions WHERE mode=?", (mode,)).fetchone()[0]
    launched = conn.execute("SELECT COUNT(*) FROM launch_requests WHERE status='launched'").fetchone()[0]
    closed = sc.get("closed") or 0
    counts = {"closed trades": closed, "smart-money calls read": sum(calls.values()), "take-profits hit": tps,
              "coins launched through the launchpad": launched}
    milestones = []
    for label, n in counts.items():
        nxt = _next(n, STEPS[label])
        if nxt == 1:
            milestones.append("my first fully closed trade, so the scorecard has a real win rate")
        elif nxt:
            milestones.append(f"{nxt} {label} (now {n})")
    win = sc.get("win_rate")
    return {
        "days_trading": (now - datetime.fromisoformat(first)).days + 1 if first else 0,
        "trades_taken": sc.get("taken") or 0, "trades_open": sc.get("open") or 0, "trades_closed": closed,
        "win_rate_pct": round(win) if closed and win is not None else "none closed yet",
        "smart_money_calls_read": counts["smart-money calls read"],
        "calls_bought": calls.get("bought", 0),
        "calls_that_never_ran_30pct_in_6h": calls.get("expired", 0),
        "calls_refused_for_a_tax_on_every_transfer": taxed,
        "take_profits_hit": tps,
        "coins_launched_for_people": launched,
        "calls_today": (site.get("funnel") or {}).get("calls_24h", 0),
        "next_milestones": milestones,
    }


def write_goals(ctx: Any, site: dict[str, Any], now: datetime) -> str:
    """Queue one goal post if one is due. Returns 'queued', 'not due', 'duplicate' or 'dropped'."""
    s, conn = ctx.settings, ctx.conn
    last = conn.execute("SELECT created_at FROM posts WHERE kind='goals' AND status NOT IN ('failed','cancelled') "
                        "ORDER BY id DESC LIMIT 1").fetchone()
    if last and now - datetime.fromisoformat(last["created_at"]) < timedelta(hours=s.x_goals_every_hours):
        return "not due"
    recent = [r["body"].removeprefix(HEADER) for r in rows(conn, "SELECT body FROM posts WHERE kind='goals' "
                                                                  "ORDER BY id DESC LIMIT 30")]
    facts = goal_facts(conn, site, s.trading_mode.value, now)
    res = ctx.brain.goals({**facts, "my_recent_goal_posts_do_not_repeat": recent})
    d = res.decision
    text = clean(d.tweet) if d else None
    if text and (len(text) > MAX_TEXT or CASHTAG.search(text) or text.lower() in {r.lower() for r in recent}):
        text = None
    if not text:
        journal_append(conn, "goals_dropped", {"draft": (d.tweet if d else "")[:300], "error": res.error})
        return "dropped"
    _, new = enqueue(conn, Draft("goals", HEADER + text, "", f"goals:{now.strftime('%Y%m%d%H%M')}"))
    return "queued" if new else "duplicate"
