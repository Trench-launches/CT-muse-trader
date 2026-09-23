import sqlite3

import pytest

from ct_agent.db import journal_append, journal_recent, journal_verify, kv_get, kv_set


def test_journal_is_append_only(db):
    jid = journal_append(db, "paper_fill", {"token": "0xabc", "qty": 1}, ref="0xabc", mode="paper")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.execute("UPDATE journal SET payload='{}' WHERE id=?", (jid,))
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.execute("DELETE FROM journal WHERE id=?", (jid,))


def test_journal_hash_chain_detects_tampering(db):
    for i in range(3):
        journal_append(db, "note", {"i": i})
    assert journal_verify(db) == (True, None)
    # Simulate someone bypassing the triggers directly on the file.
    db.execute("DROP TRIGGER journal_no_update")
    db.execute("UPDATE journal SET payload=? WHERE id=2", ('{"i": 99}',))
    ok, bad = journal_verify(db)
    assert not ok and bad == 2


def test_journal_recent_filters(db):
    journal_append(db, "a", {"x": 1}, ref="t1")
    journal_append(db, "b", {"x": 2}, ref="t2")
    assert [r["kind"] for r in journal_recent(db, ref="t2")] == ["b"]


def test_kv_roundtrip(db):
    kv_set(db, "cursor", 123)
    kv_set(db, "cursor", 456)
    assert kv_get(db, "cursor") == 456
    assert kv_get(db, "missing", "d") == "d"
