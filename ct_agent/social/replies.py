"""Auto-replies to people who mention or reply to @MooseByMuse.

Only people who engage with MOOSE first get a reply (X's automation rules), under a daily cap and a per-person
cap. Muse writes each reply, family friendly and in crypto-native language; it can decline (hostile, spam,
politics, anything unsafe), and every reply also passes a hard filter here before it's queued: no links, no
contract addresses, no tags, no bad words. Replies go through the outbox like every other post.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from ..db import journal_append, kv_get, kv_set
from .compose import Draft
from .outbox import enqueue

log = logging.getLogger(__name__)

# A hard floor under Muse's own judgement: a reply containing any of these is dropped, never edited.
BLOCKED = re.compile(
    r"\b(fuck\w*|shit\w*|bitch\w*|cunt\w*|dick\w*|cock\w*|puss(y|ies)|whore\w*|slut\w*|bastard\w*|asshole\w*|"
    r"retard\w*|nigg\w*|fag\w*|porn\w*|nsfw|sex\w*|nude\w*|kill\s+(yourself|urself)|kys|rape\w*|nazi\w*|"
    r"guarantee\w*|risk[- ]free|100x\s+guaranteed|financial advice)\b", re.I)
LINK = re.compile(r"(https?://|www\.|\b[a-z0-9-]+\.(com|io|xyz|net|org|fun|app)\b)", re.I)
ADDRESS = re.compile(r"0x[0-9a-fA-F]{6,}")


def clean(reply: str) -> str | None:
    """The reply as it will be posted, or None if it fails the hard filter."""
    text = re.sub(r"(^|\s)@\w+", r"\1", reply or "").strip()  # never tag anyone
    text = re.sub(r"\s{2,}", " ", text)
    if not text or len(text) > 260 or BLOCKED.search(text) or LINK.search(text) or ADDRESS.search(text):
        return None
    return text


def _since(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")


def _count(conn: Any, ref: str | None = None) -> int:
    q = "SELECT COUNT(*) c FROM posts WHERE kind='reply' AND status NOT IN ('failed','cancelled') AND created_at>=?"
    args: list[Any] = [_since(24)]
    if ref:
        q += " AND ref=?"
        args.append(ref)
    return int(conn.execute(q, args).fetchone()["c"])


def replies_once(ctx: Any) -> dict[str, Any]:
    s, conn = ctx.settings, ctx.conn
    if not (s.x_auto_reply_enabled and s.x_posting_enabled and ctx.x.bearer and ctx.x.can_post):
        return {"replies": "off"}
    last = kv_get(conn, "mentions_polled_at")
    if last and datetime.fromisoformat(last) > datetime.now(timezone.utc) - timedelta(minutes=s.x_mentions_poll_min):
        return {}
    kv_set(conn, "mentions_polled_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    if _count(conn) >= s.x_replies_per_day:
        return {"replies": "daily cap reached"}  # don't even pay to read mentions we can't answer
    me = kv_get(conn, "x_my_user_id")
    if not me:
        me = ctx.x.my_user_id()
        kv_set(conn, "x_my_user_id", me)
    since = kv_get(conn, "mentions_since_id")
    mentions = ctx.x.mentions(me, since_id=since, max_results=5 if since is None else 20)
    if mentions:
        kv_set(conn, "mentions_since_id", max(mentions, key=lambda m: int(m["id"]))["id"])
    if since is None:
        return {"replies": "started", "skipped_backlog": len(mentions)}  # first run: don't answer old mentions
    queued = declined = 0
    for m in sorted(mentions, key=lambda m: int(m["id"])):
        author = m.get("author_id")
        if author == me or any(r.get("type") == "retweeted" for r in m.get("referenced_tweets") or []):
            continue
        if _count(conn) >= s.x_replies_per_day:
            break
        who = f"x_user:{author}"
        if _count(conn, who) >= s.x_replies_per_user_day:
            continue
        parent = next((r["id"] for r in m.get("referenced_tweets") or [] if r.get("type") == "replied_to"), None)
        ours = conn.execute("SELECT body FROM posts WHERE x_post_id=?", (parent,)).fetchone() if parent else None
        res = ctx.brain.reply({"their_post": m.get("text", ""), "our_post_they_replied_to": ours["body"] if ours else None,
                               "now_utc": datetime.now(timezone.utc).strftime("%A %d %B %Y, %H:%M UTC"),
                               "mode": s.trading_mode.value, "trigger_pct": s.scanner_trigger_pct,
                               "moose_token_launched": bool(s.agent_token_address)})
        d = res.decision
        text = clean(d.reply) if d and not d.skip else None
        if not text:
            declined += 1
            journal_append(conn, "reply_declined", {"x_post_id": m["id"], "muse_skip": bool(d and d.skip),
                                                    "error": res.error, "draft": (d.reply if d else "")[:280]})
            continue
        _, new = enqueue(conn, Draft("reply", text, who, f"reply:{m['id']}", reply_to=f"x:{m['id']}"))
        queued += int(new)
    return {"mentions": len(mentions), "replies_queued": queued, "declined": declined}
