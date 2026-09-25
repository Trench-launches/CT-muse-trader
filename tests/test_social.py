import json
import httpx

from ct_agent.db import journal_recent, rows
from ct_agent.social import compose
from ct_agent.social.outbox import enqueue, recover_interrupted, send_pending
from ct_agent.sources.x_api import XClient

CA = "0x47A360fC5e39AAea67056F380B22428D69c794D4"


def _entry(mode="paper", pid=1, risk="one wallet is 35% of buys"):
    return compose.trade_entry(mode=mode, symbol="NEZU", token=CA, size="0.05 ETH", entry="0.0000000021 ETH",
                               catalyst="a first-party launch post (source #12) " * 3, progress=0.91,
                               momentum="buys accelerating over 5m/15m/60m", risk=risk, position_id=pid)


def test_paper_post_is_labelled_fits_and_keeps_risk_and_ca():
    d = _entry()
    assert d.body.startswith("Test buy $NEZU\n\nEntry: ") and CA not in d.body and "Simulated fill" in d.body
    assert len(d.body) <= 280
    live = compose.trade_entry(mode="live", symbol="N", token=CA, size="0.0148 ETH", entry="e", catalyst="a smart-money call",
                               progress=1.0, momentum="", risk="r", position_id=2, mc="$2.19M",
                               plan="-70% stop · 50% at 1.6x · 25% at 2x · 24h max").body
    assert live == ("Bought $N\n\nEntry: $2.19M MC\nSize: 0.0148 ETH\nExits: -70% stop · 50% at 1.6x · 25% at 2x · 24h max"
                    "\n\nSmart-money call on Robinhood Chain.")


def test_losses_are_posted():
    d = compose.trade_exit(mode="paper", symbol="N", token=CA, pnl_pct=-25.3, pnl_eth=-0.0127, sold_pct=100,
                           at="$40K MC", why="it hit my -25% stop", position_id=1, bought="first-party launch news",
                           underestimated="how thin the curve was", lesson="wait for 20+ buyer wallets")
    assert d.body.startswith("Top blasted $N in testing. Entry was poor.")
    assert "I bought because first-party launch news. Underestimated how thin the curve was." in d.body
    assert "Cut because it hit my -25% stop.\nLesson: wait for 20+ buyer wallets." in d.body
    assert d.body.endswith("Realised P&L: -0.0127 ETH (-25%).") and CA not in d.body


def test_post_formats_fit_and_use_the_ticker_only():
    long = "x " * 60
    posts = [
        compose.watch_note(symbol="HOT", token=CA, decision_id=1, narrative=long, confirmation=long, watching=long,
                           trigger=long),
        compose.pass_note(symbol="HOT", token=CA, decision_id=2, narrative=long, concern=long, reconsider_if=long),
        compose.take_profit(mode="paper", symbol="HOT", token=CA, sold_pct=50, at="$92.2K MC", why=long, left_pct=50,
                            realized_eth=0.0173, position_id=1, rung=1),
        compose.trade_exit(mode="paper", symbol="HOT", token=CA, pnl_pct=-40, pnl_eth=-0.02, sold_pct=100, at="$9K MC",
                           why=long, position_id=1, bought=long, underestimated=long, lesson=long),
        compose.trade_exit(mode="paper", symbol="HOT", token=CA, pnl_pct=80, pnl_eth=0.04, sold_pct=25, at="$1.2M MC",
                           why="it hit my final target at 8.1x", position_id=1, last_bit=True),
    ]
    for d in posts:
        assert len(d.body) <= 280 and CA not in d.body and "0x" not in d.body, d.body  # tickers only
    w = compose.watch_note(symbol="HOT", token=CA, decision_id=1, narrative="HATCH v1 launch news",
                           confirmation="more wallets buying", watching="graduation progress",
                           trigger="it clears 90% with steady buys")
    assert w.body.startswith("$HOT is on my radar. No position.\n\nHATCH v1 launch news caught my attention, "
                             "but I want more wallets buying before buying.\n\nWatching graduation progress. If it "
                             "clears 90% with steady buys, I take a starter. Until then, happy to wait.")
    assert posts[4].body.startswith("Jeeted the last 25% of my $HOT test bag at $1.2M MC.")
    assert "Fully out. Realised P&L: +0.0400 ETH (+80%)." in posts[4].body


def test_own_token_disclosed():
    d = compose.trade_entry(mode="live", symbol="CT", token=CA, size="s", entry="e", catalyst="c", progress=0.9,
                            momentum="", risk="r", position_id=3, own_token=True)
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
    d = compose.pass_note(symbol="T", token=CA, line="No real news is driving this.", decision_id=1)
    assert ".." not in d.body and d.body.startswith("Faded $T.\n\nNo real news is driving this.")
    d = compose.pass_note(symbol="T", token=CA, decision_id=1, narrative="launch news.", concern="one wallet buying.",
                          reconsider_if="buys spread out.")
    assert ".." not in d.body and "I see the launch news thesis. Couldn't justify the entry with one wallet buying." in d.body


def test_entry_post_never_says_none_or_unmeasured_momentum():
    from ct_agent.strategy.engine import _describe_source
    src = {"kind": "news", "author": None, "channel": None, "origin_url": "https://www.decrypt.co/x", "confidence": "first_party"}
    assert _describe_source(src) == "a first-party news post from decrypt.co"
    d = compose.trade_entry(mode="paper", symbol="T", token=CA, size="0.05 ETH", entry="1e-9 ETH", catalyst="a source",
                            progress=1.0, momentum="", risk="thin pool", position_id=9)
    assert "None" not in d.body and "accelerating" not in d.body


def test_trade_posts_go_before_commentary_and_replies_wait_for_parent(db):
    enqueue(db, compose.watch_note(symbol="W", token=CA, line="t", decision_id=1))
    enqueue(db, _entry(pid=1))
    enqueue(db, compose.position_update(mode="paper", symbol="NEZU", token=CA, pnl_pct=26, price="1e-9 ETH",
                                        milestone=25, plan="Stop -25%, first take-profit at 1.6x.", position_id=1))
    got: list[dict] = []

    def h(r):
        got.append(json.loads(r.content))
        return httpx.Response(201, json={"data": {"id": str(len(got))}})
    send_pending(db, _x(h), enabled=True, daily_cap=60)
    assert [g["text"][:9] for g in got] == ["Test buy ", "$NEZU upd", "$W is on "]
    assert got[1]["reply"] == {"in_reply_to_tweet_id": "1"} and "reply" not in got[0]


def test_no_replies_under_a_previous_x_account(db):
    """After the posting account changes, follow-ups to the old account's posts go out on their own."""
    enqueue(db, _entry(pid=1))
    db.execute("UPDATE posts SET status='sent', x_post_id='old1', sent_at='2026-09-24T10:00:00+00:00'")
    enqueue(db, compose.position_update(mode="paper", symbol="NEZU", token=CA, pnl_pct=26, price="1e-9 ETH",
                                        milestone=25, plan="Stop -30%.", position_id=1))
    got: list[dict] = []

    def h(r):
        got.append(json.loads(r.content))
        return httpx.Response(201, json={"data": {"id": str(len(got))}})
    send_pending(db, _x(h), enabled=True, daily_cap=60, threads_since="2026-09-24T19:19:00+00:00")
    assert len(got) == 1 and "reply" not in got[0]  # the parent is on the old account
    enqueue(db, _entry(pid=2))
    send_pending(db, _x(h), enabled=True, daily_cap=60, threads_since="2026-09-24T19:19:00+00:00")
    enqueue(db, compose.position_update(mode="paper", symbol="NEZU", token=CA, pnl_pct=26, price="1e-9 ETH",
                                        milestone=25, plan="Stop -30%.", position_id=2))
    send_pending(db, _x(h), enabled=True, daily_cap=60, threads_since="2026-09-24T19:19:00+00:00")
    assert got[-1]["reply"] == {"in_reply_to_tweet_id": "2"}  # a thread on the new account still threads


def test_usd_compact():
    assert compose.usd_compact(42_130) == "$42.1K" and compose.usd_compact(1_234_000) == "$1.23M"
    assert compose.usd_compact(None) == "" and compose.usd_compact(950) == "$950"


def test_position_update_fits_the_thesis_in_280():
    long = "the story spread from one account to dozens of posts and new wallets keep buying the dips " * 3
    d = compose.position_update(mode="live", symbol="NEZU", token=CA, pnl_pct=104, price="2.1e-7 ETH", milestone=100,
                                plan="Stop -70%, first take-profit at 1.6x.", position_id=4, mc="$212K",
                                thesis=long)
    assert len(d.body) <= 280 and "\n\nUpdated thesis: the story spread" in d.body and d.body.count("..") == 0
    assert d.body.startswith("$NEZU update: +104% since entry\nMC now: $212K\n\nUpdated thesis: ")
    assert d.body.endswith("\n\nStill holding. Stop -70%, first take-profit at 1.6x.")
    plain = compose.position_update(mode="live", symbol="NEZU", token=CA, pnl_pct=-15, price="1e-9 ETH",
                                    milestone=-15, plan="Stop -70%.", position_id=4)
    assert "Updated thesis" not in plain.body


def test_buy_read_fits_muses_long_reasons_in_one_post():
    reasons = ["51 unique buyers in 1h and 16 in 5m with acceleration 3.28 shows broad speeding buys despite 29% top "
               "buyer share", "holders dispersed 201 holders top10 17.26% and top wallet 3.02% with deployer 0% so no "
               "holder concentration", "X attention only 4 mentions of exact address versus 9.18 ETH buys in 1h and "
               "44k market cap so underfollowed"]
    d = compose.buy_read(symbol="RAINEGG", token=CA, key="analysis:375", reply_to="entry:live:7", reasons=reasons,
                         risk="37 roundtrip traders flag suggests wash inflated volume and top buyer 29% could dump")
    assert len(d.body) <= 280 and d.body.startswith("Why I bought $RAINEGG:\n\n• 51 unique buyers")
    assert d.body.count("\n• ") >= 2 and "\n\nRisk: 37 roundtrip traders" in d.body
    assert (d.kind, d.reply_to) == ("analysis", "entry:live:7")
