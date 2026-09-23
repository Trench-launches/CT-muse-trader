import httpx

from ct_agent.db import journal_recent, rows
from ct_agent.social import compose
from ct_agent.social.outbox import enqueue, recover_interrupted, send_pending
from ct_agent.sources.x_api import XClient

CA = "0x47A360fC5e39AAea67056F380B22428D69c794D4"


def _entry(mode="paper", pid=1, risk="one wallet is 35% of buys"):
    return compose.trade_entry(mode=mode, symbol="NEZU", token=CA, size="0.05 ETH", entry="0.0000000021 ETH",
                               catalyst="a first-party launch post (source #12) " * 3, progress=0.91,
                               window="5m/15m/1h", risk=risk, position_id=pid)


def test_paper_post_is_labelled_fits_and_keeps_risk_and_ca():
    d = _entry()
    assert d.body.startswith("PAPER BUY $NEZU") and CA in d.body and "Risk:" in d.body
    assert len(d.body) <= 280
    assert compose.trade_entry(mode="live", symbol="N", token=CA, size="s", entry="e", catalyst="c", progress=0.9,
                               window="w", risk="r", position_id=2).body.startswith("BOUGHT")


def test_losses_are_posted():
    d = compose.trade_exit(mode="paper", symbol="N", token=CA, pnl_pct=-25.3, exit_price="x", reason="stop loss",
                           position_id=1)
    assert "-25.3%" in d.body and "Loss logged" in d.body and "PAPER SOLD" in d.body


def test_own_token_disclosed():
    d = compose.trade_entry(mode="live", symbol="CT", token=CA, size="s", entry="e", catalyst="c", progress=0.9,
                            window="w", risk="r", position_id=3, own_token=True)
    assert "Disclosure: this is my own project token." in d.body


def _x(handler):
    return XClient(user_token="user", transport=httpx.MockTransport(handler))


def test_outbox_idempotent_and_daily_cap(db):
    for i in range(8):
        enqueue(db, _entry(pid=i))
    assert enqueue(db, _entry(pid=0))[1] is False  # same key, not re-queued
    n = iter(range(100))
    res = send_pending(db, _x(lambda r: httpx.Response(201, json={"data": {"id": str(next(n)), "text": "t"}})),
                       enabled=True, daily_cap=6)
    assert len(res) == 6 and all(r["status"] == "sent" for r in res)
    assert len(rows(db, "SELECT * FROM posts WHERE status='pending'")) == 2
    assert len(journal_recent(db, kind="post_sent")) == 6


def test_disabled_posting_keeps_drafts(db):
    enqueue(db, _entry())
    assert send_pending(db, _x(lambda r: httpx.Response(500)), enabled=False, daily_cap=6) == []
    assert rows(db, "SELECT status FROM posts")[0]["status"] == "pending"


def test_timeout_marks_unknown_not_retried(db):
    enqueue(db, _entry())

    def boom(r):
        raise httpx.ReadTimeout("slow", request=r)
    res = send_pending(db, _x(boom), enabled=True, daily_cap=6)
    assert res[0]["status"] == "unknown"
    ok = _x(lambda r: httpx.Response(201, json={"data": {"id": "9"}}))
    assert send_pending(db, ok, enabled=True, daily_cap=6) == []  # not blindly retried


def test_retryable_and_permanent_errors(db):
    enqueue(db, _entry(pid=1))
    assert send_pending(db, _x(lambda r: httpx.Response(429)), enabled=True, daily_cap=6)[0]["status"] == "pending"
    assert send_pending(db, _x(lambda r: httpx.Response(403, text="dup")), enabled=True, daily_cap=6)[0]["status"] \
        == "failed"


def test_crash_recovery_marks_unknown(db):
    enqueue(db, _entry())
    db.execute("UPDATE posts SET status='sending'")
    assert recover_interrupted(db) == 1
    assert rows(db, "SELECT status FROM posts")[0]["status"] == "unknown"


def test_no_double_full_stops():
    d = compose.pass_note(symbol="T", token=CA, reason="No real news is driving this.", decision_id=1)
    assert ".." not in d.body and d.body.endswith("No real news is driving this.")
