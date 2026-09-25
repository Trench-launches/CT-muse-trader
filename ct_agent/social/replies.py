"""Auto-replies to people who mention or reply to @MooseByMuse.

Only people who engage with MOOSE first get a reply (X's automation rules), under a daily cap and a per-person
cap. Muse writes each reply, family friendly and in crypto-native language; it can decline (hostile, spam,
politics, anything unsafe), and every reply also passes a hard filter here before it's queued: no links, no
contract addresses, no tags, no bad words. Replies go through the outbox like every other post.
"""
from __future__ import annotations

import json
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


def rules(s: Any) -> str:
    """How MOOSE trades, from its live settings, so a reply never quotes an old rule."""
    buy = ("buys every tradeable smart-money call the moment it lands" if s.scanner_buy_on_call
           else f"buys a smart-money call once it runs +{s.scanner_trigger_pct:g}% from the call")
    if s.pons_scan_enabled:
        buy += ", and also coins its own Pons scan picks when Muse likes the story and the buying"
    tps = ", ".join(f"{f * 100:g}% at {m:g}x" for m, f in sorted(s.take_profit_ladder))
    return f"MOOSE {buy}. Exits: a -{s.stop_loss_pct:g}% stop, take-profits ({tps}), and a {s.max_hold_hours:g}h timer."


def _coin(ctx: Any, token: str) -> dict[str, Any] | None:
    """What MOOSE knows about the coin behind the post they replied to: its own story and MOOSE's thesis."""
    conn = ctx.conn
    row = conn.execute("SELECT symbol, name FROM launches WHERE lower(token)=lower(?)", (token,)).fetchone()
    d = conn.execute("SELECT thesis, raw FROM decisions WHERE lower(token)=lower(?) AND raw IS NOT NULL AND raw!='' "
                     "ORDER BY id DESC LIMIT 1", (token,)).fetchone()
    try:
        raw = json.loads(d["raw"]) if d else {}
    except ValueError:
        raw = {}
    try:
        from ..sources.lore import get_lore
        lore = get_lore(ctx, token, look=False)  # cached or read onchain: no extra X or web calls for a reply
    except Exception as exc:
        log.warning("lore unavailable for %s: %s", token, exc)
        lore = {}
    return {"symbol": row["symbol"] if row else None, "name": row["name"] if row else None,
            "moose_thesis": raw.get("public_thesis") or (raw.get("narrative") or {}).get("take") or (d["thesis"] if d else None),
            "public_line": raw.get("public_line"), "coins_own_lore": lore}


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
        ours = conn.execute("SELECT body, ref FROM posts WHERE x_post_id=?", (parent,)).fetchone() if parent else None
        coin = _coin(ctx, ours["ref"]) if ours and re.fullmatch(r"0x[0-9a-fA-F]{40}", ours["ref"] or "") else None
        res = ctx.brain.reply({"their_post": m.get("text", ""), "our_post_they_replied_to": ours["body"] if ours else None,
                               "coin": coin, "now_utc": datetime.now(timezone.utc).strftime("%A %d %B %Y, %H:%M UTC"),
                               "mode": s.trading_mode.value, "rules": rules(s),
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
