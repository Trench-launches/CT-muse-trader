"""RSS/Atom news ingestion.

Keeps the first-party link and publication time. Feeds are configured as
"url" or "url|first_party". Items from a feed flagged first_party (e.g. a
project's own blog, an exchange's newsroom) get confidence first_party;
everything else is "reported".
"""
from __future__ import annotations

import calendar
import html
import logging
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable

import feedparser
import httpx

from .store import SourceIn, add_source

log = logging.getLogger(__name__)


def _plain(html_text: str) -> str:
    """Feed summaries are often HTML; keep only readable text for storage and the AI."""
    text = re.sub(r"<(script|style)\b.*?</\1>", " ", html_text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def parse_feed_spec(spec: str) -> tuple[str, bool]:
    url, _, flag = spec.partition("|")
    return url.strip(), flag.strip().lower() == "first_party"


def _published(entry: Any) -> str | None:
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    if not t:
        return None
    return datetime.fromtimestamp(calendar.timegm(t), tz=timezone.utc).isoformat(timespec="seconds")


def ingest_feed(conn: sqlite3.Connection, spec: str,
                fetch: Callable[[str], bytes] | None = None) -> int:
    url, first_party = parse_feed_spec(spec)
    if fetch is None:
        def fetch(u: str) -> bytes:
            r = httpx.get(u, timeout=20, follow_redirects=True, headers={"user-agent": "ct-agent/0.1"})
            r.raise_for_status()
            return r.content
    try:
        parsed = feedparser.parse(fetch(url))
    except httpx.HTTPError as exc:
        log.warning("feed %s failed: %s", url, exc)
        return 0
    created = 0
    for e in parsed.entries:
        link = e.get("link")
        text = " ".join(filter(None, [e.get("title"), _plain(e.get("summary") or "")]))
        if not link or not text:
            continue
        _, new = add_source(conn, SourceIn(
            kind="news", origin_url=link, origin_id=e.get("id") or link,
            author=e.get("author") or parsed.feed.get("title"), published_at=_published(e),
            excerpt=text[:2000], confidence="first_party" if first_party else "reported",
            cites_url=link, raw={"feed": url},
        ))
        created += int(new)
    return created
