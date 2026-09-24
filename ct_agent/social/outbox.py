"""X outbox: queue, daily cap, idempotency, safe retries.

Order: trade posts (entries, updates, exits, corrections, scorecards) go
before commentary (watch/pass). A reply waits until the post it threads
under has been sent; if that parent failed, the reply goes out on its own.

States: pending -> sending -> sent
                           -> pending (retryable error: 429/5xx, attempts < max)
                           -> failed  (permanent 4xx or too many attempts)
                           -> unknown (timeout / crash mid-send: the post may
                                       exist; a human must check before any
                                       retry, so we never blindly double-post)
The social process is separate from trading: if X is down, posts wait here
and trading continues with every fill already journalled.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from ..db import journal_append, rows, tx, utcnow
from ..sources.x_api import XClient, XNotConfigured
from .compose import Draft

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 5
# Trade posts go first, in the order they happened; anything else (watch, pass) is commentary.
TRADE_KINDS = {"trade_entry", "update", "take_profit", "trade_exit", "correction", "scorecard"}


def enqueue(conn: sqlite3.Connection, d: Draft) -> tuple[int, bool]:
    existing = conn.execute("SELECT id FROM posts WHERE idempotency_key=?", (d.idempotency_key,)).fetchone()
    if existing:
        return int(existing["id"]), False
    cur = conn.execute("INSERT INTO posts(idempotency_key,kind,body,ref,created_at,reply_to_key) VALUES(?,?,?,?,?,?)",
                       (d.idempotency_key, d.kind, d.body, d.ref, utcnow(), d.reply_to))
    journal_append(conn, "post_queued", {"post_id": cur.lastrowid, "kind": d.kind, "body": d.body,
                                         "key": d.idempotency_key}, ref=d.ref or None)
    return int(cur.lastrowid), True


def sent_last_24h(conn: sqlite3.Connection) -> int:
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
    return int(conn.execute("SELECT COUNT(*) c FROM posts WHERE status IN ('sent','sending','unknown') "
                            "AND COALESCE(sent_at, created_at) >= ?", (since,)).fetchone()["c"])


def recover_interrupted(conn: sqlite3.Connection) -> int:
    """At startup: anything left 'sending' was interrupted mid-call."""
    stuck = rows(conn, "SELECT id FROM posts WHERE status='sending'")
    for r in stuck:
        conn.execute("UPDATE posts SET status='unknown', last_error='interrupted mid-send; verify on X' WHERE id=?",
                     (r["id"],))
        journal_append(conn, "post_unknown", {"post_id": r["id"], "why": "interrupted mid-send"})
    return len(stuck)


def send_pending(conn: sqlite3.Connection, x: XClient, *, enabled: bool, daily_cap: int,
                 threads_since: str = "") -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    if not enabled:
        return results
    while True:
        if sent_last_24h(conn) >= daily_cap:
            break
        with tx(conn):
            picked = _next_sendable(conn, threads_since)
            if not picked:
                break
            row, parent_x_id = picked
            conn.execute("UPDATE posts SET status='sending', attempts=attempts+1, sent_at=? WHERE id=?",
                         (utcnow(), row["id"]))
        results.append(_send_one(conn, x, row, parent_x_id))
        if results[-1]["status"] not in ("sent",):
            break  # back off on any problem; next cycle retries
    return results


def _next_sendable(conn: sqlite3.Connection, threads_since: str = "") -> tuple[dict[str, Any], str | None] | None:
    """Highest-priority pending post whose thread parent (if any) is settled."""
    pending = rows(conn, "SELECT * FROM posts WHERE status='pending'")
    pending.sort(key=lambda r: (r["kind"] not in TRADE_KINDS, r["id"]))
    for row in pending:
        if not row.get("reply_to_key"):
            return row, None
        if row["reply_to_key"].startswith("x:"):  # an auto-reply: straight under someone else's post
            return row, row["reply_to_key"][2:]
        parent = conn.execute("SELECT status, x_post_id, sent_at FROM posts WHERE idempotency_key=?",
                              (row["reply_to_key"],)).fetchone()
        if parent is None or parent["status"] in ("failed", "cancelled", "unknown"):
            return row, None  # nothing to thread under: post it on its own
        if parent["status"] == "sent":
            if threads_since and (parent["sent_at"] or "") < threads_since:
                return row, None  # the parent is on a previous X account: don't reply under it
            return row, parent["x_post_id"]
        # parent still pending/sending: wait so the thread reads in order
    return None


def _send_one(conn: sqlite3.Connection, x: XClient, row: dict[str, Any], reply_to: str | None = None) -> dict[str, Any]:
    pid, attempts = row["id"], row["attempts"] + 1
    try:
        res = x.create_post(row["body"], reply_to=reply_to)
    except XNotConfigured as exc:
        return _set(conn, pid, "pending", str(exc), attempts=row["attempts"])
    except httpx.TimeoutException as exc:
        return _set(conn, pid, "unknown", f"timeout, post may exist: {exc}")
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code == 429 or code >= 500:
            state = "failed" if attempts >= MAX_ATTEMPTS else "pending"
            return _set(conn, pid, state, f"http {code}")
        return _set(conn, pid, "failed", f"http {code}: {exc.response.text[:200]}")
    except httpx.TransportError as exc:  # connection never established
        return _set(conn, pid, "pending" if attempts < MAX_ATTEMPTS else "failed", f"transport: {exc}")
    conn.execute("UPDATE posts SET status='sent', x_post_id=?, sent_at=?, last_error=NULL WHERE id=?",
                 (res.post_id, utcnow(), pid))
    journal_append(conn, "post_sent", {"post_id": pid, "x_post_id": res.post_id, "body": row["body"],
                                       "kind": row["kind"]}, ref=row["ref"] or None)
    return {"id": pid, "status": "sent", "x_post_id": res.post_id}


def _set(conn: sqlite3.Connection, pid: int, status: str, err: str, attempts: int | None = None) -> dict[str, Any]:
    if attempts is not None:
        conn.execute("UPDATE posts SET status=?, last_error=?, attempts=? WHERE id=?", (status, err, attempts, pid))
    else:
        conn.execute("UPDATE posts SET status=?, last_error=? WHERE id=?", (status, err, pid))
    if status in ("failed", "unknown"):
        journal_append(conn, f"post_{status}", {"post_id": pid, "error": err})
    return {"id": pid, "status": status, "error": err}
