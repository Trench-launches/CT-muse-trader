"""Research process: news feeds, lead verification, budgeted X lookups.

X reads are paid, so lookups only run for tokens that already passed the
onchain screen, and search by exact contract address so posts about a
different token with the same ticker are not mixed in.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from ..context import Context
from ..db import kv_get, kv_set, utcnow
from ..sources.news import ingest_feed
from ..sources.store import verify_leads
from ..sources.x_api import XBudgetExceeded, XNotConfigured

log = logging.getLogger(__name__)


def research_once(ctx: Context) -> dict[str, Any]:
    s, conn = ctx.settings, ctx.conn
    out: dict[str, Any] = {"news": 0, "x_posts": 0, "attention": 0, "leads": 0, "errors": []}
    for spec in s.news_feeds:
        try:
            out["news"] += ingest_feed(conn, spec)
        except Exception as exc:  # one bad feed must not stop the rest
            out["errors"].append(f"feed {spec}: {exc}")

    if s.x_bearer_token:
        for c in (kv_get(conn, "candidates") or [])[:3]:
            token = c["token"]
            key = f"x_attention:{token.lower()}"
            prev = kv_get(conn, key)
            if prev and prev.get("measured_at", "") > _minutes_ago(60):
                continue
            try:
                count = ctx.x.mention_counts(f'"{token}"')
                out["x_posts"] += ctx.x.ingest_search(conn, f'"{token}" -is:retweet', s.x_max_reads_per_search)
                kv_set(conn, key, {"count_24h": count, "measured_at": utcnow(), "query": "exact contract address"})
                out["attention"] += 1
            except XBudgetExceeded as exc:
                out["errors"].append(str(exc))
                break
            except (XNotConfigured, httpx.HTTPError) as exc:
                out["errors"].append(f"x: {exc}")
                break

    out["leads"] = len(verify_leads(conn, ctx.pons))
    return out


def _minutes_ago(m: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=m)).isoformat(timespec="seconds")
