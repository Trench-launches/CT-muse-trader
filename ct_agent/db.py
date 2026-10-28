"""SQLite storage with an append-only, hash-chained journal.

SQLite in WAL mode handles several worker processes on one host. Swap for
Postgres when the system needs more than one machine.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

GENESIS_HASH = "0" * 64
_SCHEMA = Path(__file__).with_name("schema.sql")
_journal_lock = threading.Lock()


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str) -> sqlite3.Connection:
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA.read_text(encoding="utf-8"))
    # Columns added after the first release (CREATE TABLE IF NOT EXISTS won't add them).
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(posts)")}
    if "reply_to_key" not in cols:
        conn.execute("ALTER TABLE posts ADD COLUMN reply_to_key TEXT")
    if "media" not in cols:
        conn.execute("ALTER TABLE posts ADD COLUMN media TEXT")
    scols = {r["name"] for r in conn.execute("PRAGMA table_info(scanner_calls)")}
    if "decision_id" not in scols:
        conn.execute("ALTER TABLE scanner_calls ADD COLUMN decision_id INTEGER")
    pcols = {r["name"] for r in conn.execute("PRAGMA table_info(positions)")}
    for col, ddl in (("tokens_left", "REAL"), ("realized_quote", "REAL NOT NULL DEFAULT 0"),
                     ("tp_hits", "INTEGER NOT NULL DEFAULT 0")):
        if col not in pcols:
            conn.execute(f"ALTER TABLE positions ADD COLUMN {col} {ddl}")
    if "image_uri" not in {r["name"] for r in conn.execute("PRAGMA table_info(launch_requests)")}:
        conn.execute("ALTER TABLE launch_requests ADD COLUMN image_uri TEXT")


def open_db(path: str) -> sqlite3.Connection:
    conn = connect(path)
    migrate(conn)
    return conn


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Explicit write transaction. BEGIN IMMEDIATE takes the write lock up
    front so concurrent workers queue instead of deadlocking."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


# --- key/value --------------------------------------------------------------

def kv_get(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def kv_set(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value)),
    )


# --- journal ----------------------------------------------------------------

def _row_hash(prev_hash: str, ts: str, kind: str, ref: str | None, mode: str | None, payload: str) -> str:
    material = "|".join([prev_hash, ts, kind, ref or "", mode or "", payload])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _append_locked(conn: sqlite3.Connection, kind: str, body: str, ref: str | None, mode: str | None) -> int:
    last = conn.execute("SELECT hash FROM journal ORDER BY id DESC LIMIT 1").fetchone()
    prev = last["hash"] if last else GENESIS_HASH
    ts = utcnow()
    h = _row_hash(prev, ts, kind, ref, mode, body)
    cur = conn.execute(
        "INSERT INTO journal(ts,kind,ref,mode,payload,prev_hash,hash) VALUES(?,?,?,?,?,?,?)",
        (ts, kind, ref, mode, body, prev, h),
    )
    return int(cur.lastrowid)


def journal_append(
    conn: sqlite3.Connection,
    kind: str,
    payload: dict[str, Any],
    *,
    ref: str | None = None,
    mode: str | None = None,
    in_tx: bool = False,
) -> int:
    """Append one entry. The write lock plus BEGIN IMMEDIATE keep the hash
    chain linear across threads and processes. Pass in_tx=True when already
    inside a tx() block so the entry commits atomically with other writes."""
    body = json.dumps(payload, sort_keys=True, default=str)
    if in_tx:
        return _append_locked(conn, kind, body, ref, mode)
    with _journal_lock, tx(conn):
        return _append_locked(conn, kind, body, ref, mode)


def journal_verify(conn: sqlite3.Connection) -> tuple[bool, int | None]:
    """Recompute the chain. Returns (ok, first_bad_id)."""
    prev = GENESIS_HASH
    for r in conn.execute("SELECT * FROM journal ORDER BY id"):
        if r["prev_hash"] != prev:
            return False, r["id"]
        if _row_hash(prev, r["ts"], r["kind"], r["ref"], r["mode"], r["payload"]) != r["hash"]:
            return False, r["id"]
        prev = r["hash"]
    return True, None


def journal_recent(conn: sqlite3.Connection, *, kind: str | None = None, ref: str | None = None,
                   limit: int = 50) -> list[dict[str, Any]]:
    q = "SELECT * FROM journal WHERE 1=1"
    args: list[Any] = []
    if kind:
        q += " AND kind=?"
        args.append(kind)
    if ref:
        q += " AND ref=?"
        args.append(ref)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    out = []
    for r in conn.execute(q, args):
        d = dict(r)
        d["payload"] = json.loads(d["payload"])
        out.append(d)
    return out


def rows(conn: sqlite3.Connection, sql: str, args: tuple | list = ()) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, args)]
