"""End-to-end: fake chain + fake Muse + fake X, real code in between.

index -> research (lead verification) -> trader (screen, Muse, gates,
paper fill, post) -> social (send) -> API reads it all back.
"""
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from ct_agent.api.app import create_app
from ct_agent.config import Settings
from ct_agent.context import build
from ct_agent.db import journal_recent, journal_verify, rows
from ct_agent.sources.store import SourceIn, add_source
from ct_agent.workers.indexer import index_once
from ct_agent.workers.research import research_once
from ct_agent.workers.runner import social_once, trader_once

from .fakes import FakeChain, FakeToken, addr

ETH = 10**18


def _scenario() -> tuple[FakeChain, FakeToken, FakeToken]:
    ch = FakeChain(head=1_000_000)
    hot = FakeToken(token=addr(0xA1), curve=addr(0xC1), deployer=addr(0xD1), name="Hot", symbol="HOT")
    cold = FakeToken(token=addr(0xA2), curve=addr(0xC2), deployer=addr(0xD2), name="Cold", symbol="COLD")
    ch.launch(hot, 900_000)
    ch.launch(cold, 900_001)
    hot.real_quote = int(3.5 * ETH)
    # accelerating buys from many wallets: sparse an hour ago, dense now
    blocks = [ch.head - 30_000, ch.head - 20_000, ch.head - 6_000] + [ch.head - 2_500 + i * 300 for i in range(8)]
    for i, b in enumerate(blocks):
        ch.trade(hot, b, "buy", int(0.05 * ETH), 10**24, addr(0xB000 + i))
    ch.trade(cold, ch.head - 1_000, "buy", int(0.01 * ETH), 10**22, addr(0xBEEF), log_index=1)
    return ch, hot, cold


def _settings(tmp_path, **kw) -> Settings:
    base = dict(_env_file=None, database_path=str(tmp_path / "e2e.db"), kill_switch_file=str(tmp_path / "KILL"),
                x_posting_enabled=True, x_user_access_token="user-token", api_admin_token="admin-secret",
                decision_cooldown_min=0)
    base.update(kw)
    return Settings(**base)


def _muse(action="buy", ids=None):
    calls = []

    def complete(instructions, text, schema, name):
        calls.append(text)
        if name == "exit_review":
            return json.dumps({"action": "hold", "confidence": 0.3, "reasons": ["thesis intact"]})
        ev = ids if ids is not None else [int(x) for x in _ids_in(text)]
        return json.dumps({"action": action, "confidence": 0.72, "thesis": "first-party news plus accelerating buys",
                           "reasons": ["verified news", "buys accelerating across 5m/15m/1h"],
                           "risks": ["one-hour spike may fade after graduation"], "evidence_ids": ev,
                           "underfollowed": None, "post_angle": "news + curve"})
    complete.calls = calls
    return complete


def _ids_in(text: str) -> list[str]:
    ev = text.split("<untrusted_evidence>")[1]
    return [json.loads(line)["id"] for line in ev.splitlines() if line.startswith("{")]


def _x_transport(sent: list):
    def h(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path == "/2/tweets":
            sent.append(json.loads(req.content)["text"])
            return httpx.Response(201, json={"data": {"id": str(1000 + len(sent)), "text": sent[-1]}})
        return httpx.Response(404)
    return httpx.MockTransport(h)


def _market():
    return httpx.MockTransport(lambda r: httpx.Response(200, json={"ethereum": {"usd": 2700.0}}))


def test_full_paper_cycle(tmp_path):
    ch, hot, cold = _scenario()
    sent: list[str] = []
    muse = _muse()
    ctx = build(_settings(tmp_path), rpc_transport=ch.transport(), market_transport=_market(),
                x_transport=_x_transport(sent), completer=muse)

    idx = index_once(ctx)
    assert idx["launches"] == 2 and idx["trades"] == 12 and idx["refreshed"] == 2
    lh = rows(ctx.conn, "SELECT last_progress FROM launches WHERE token=?", (hot.token,))[0]
    assert lh["last_progress"] >= 0.85

    # first-party news naming the exact contract, plus a rumor and chat
    add_source(ctx.conn, SourceIn(kind="news", origin_id="n1", origin_url="https://blog.example/hot",
                                  excerpt=f"HOT team ships v1 on Robinhood Chain, contract {hot.token}",
                                  confidence="first_party", cites_url="https://blog.example/hot"))
    add_source(ctx.conn, SourceIn(kind="twitch_chat", origin_id="c1", excerpt=f"{hot.token} to the moon",
                                  confidence="sentiment"))
    r = research_once(ctx)
    assert r["leads"] == 2

    out = trader_once(ctx)
    d = [x for x in out["decisions"] if x["token"] == hot.token][0]
    assert d["action"] == "buy" and d.get("entered"), d
    assert "<untrusted_evidence>" in muse.calls[0] and "HOT team ships" in muse.calls[0]

    pos = rows(ctx.conn, "SELECT * FROM positions")
    assert len(pos) == 1 and pos[0]["mode"] == "paper" and pos[0]["entry_quote"] == pytest.approx(0.05)
    fill = journal_recent(ctx.conn, kind="paper_fill")[0]["payload"]
    assert fill["onchain"] is False and fill["pricing"] == "curve_constant_product"

    social_once(ctx)
    assert len(sent) == 1 and sent[0].startswith("PAPER BUY $HOT") and hot.token in sent[0]
    assert "Risk:" in sent[0] and len(sent[0]) <= 280
    assert "on Robinhood Chain at $" in sent[0] and " MC: " in sent[0], sent[0]

    # second cycle: position held, no duplicate entry or post
    out2 = trader_once(ctx)
    assert out2["exits"][0]["action"] == "hold"
    assert rows(ctx.conn, "SELECT COUNT(*) c FROM positions")[0]["c"] == 1

    # price collapses -> stop loss -> exit posted, loss logged
    hot.real_quote = int(0.2 * ETH)
    out3 = trader_once(ctx)
    assert out3["exits"][0]["action"] == "exit" and "stop loss" in out3["exits"][0]["reason"]
    social_once(ctx)
    assert sent[-1].startswith("PAPER SOLD $HOT") and "Loss logged" in sent[-1]
    assert journal_verify(ctx.conn) == (True, None)

    # API reads it back; admin needs the token
    api = TestClient(create_app(ctx.settings))
    h = api.get("/health").json()
    assert h["mode"] == "paper" and h["journal"]["chain_intact"] and h["integrations"]["muse"] is False
    assert api.get("/scorecard").json()["losses"] == 1
    assert api.get(f"/tokens/{hot.token}").json()["positions"][0]["status"] == "closed"
    assert api.post("/admin/kill", json={"on": True, "reason": "test stop"}).status_code == 401
    assert api.post("/admin/kill", json={"on": True, "reason": "test stop"},
                    headers={"authorization": "Bearer admin-secret"}).status_code == 200
    assert api.get("/health").json()["kill_switch"]["on"] is True


def test_rumor_only_cannot_trigger_buy(tmp_path):
    ch, hot, _ = _scenario()
    ctx = build(_settings(tmp_path), rpc_transport=ch.transport(), market_transport=_market(),
                x_transport=_x_transport([]), completer=_muse())
    index_once(ctx)
    add_source(ctx.conn, SourceIn(kind="x", origin_id="x1", origin_url="https://x.com/a/status/1",
                                  excerpt=f"heard {hot.token} gets listed", confidence="rumor"))
    research_once(ctx)
    d = [x for x in trader_once(ctx)["decisions"] if x["token"] == hot.token][0]
    assert d["action"] == "watch" and any("source" in g for g in d["gates"])
    assert rows(ctx.conn, "SELECT COUNT(*) c FROM positions")[0]["c"] == 0


def test_muse_citing_unknown_evidence_is_gated(tmp_path):
    ch, hot, _ = _scenario()
    ctx = build(_settings(tmp_path), rpc_transport=ch.transport(), market_transport=_market(),
                x_transport=_x_transport([]), completer=_muse(ids=[999]))
    index_once(ctx)
    add_source(ctx.conn, SourceIn(kind="news", origin_id="n1", origin_url="https://blog.example/hot",
                                  excerpt=f"contract {hot.token}", confidence="first_party"))
    research_once(ctx)
    d = [x for x in trader_once(ctx)["decisions"] if x["token"] == hot.token][0]
    assert d["action"] == "watch" and any("not provided" in g for g in d["gates"])


def test_no_evidence_passes_without_calling_muse(tmp_path):
    ch, hot, _ = _scenario()
    muse = _muse()
    ctx = build(_settings(tmp_path), rpc_transport=ch.transport(), market_transport=_market(),
                x_transport=_x_transport([]), completer=muse)
    index_once(ctx)
    d = [x for x in trader_once(ctx)["decisions"] if x["token"] == hot.token][0]
    assert d["action"] == "pass" and muse.calls == []
    row = rows(ctx.conn, "SELECT model, reasons FROM decisions WHERE token=?", (hot.token,))[0]
    assert row["model"] == "rule" and "no source" in row["reasons"]
    assert rows(ctx.conn, "SELECT COUNT(*) c FROM posts")[0]["c"] == 0


def test_kill_switch_stops_new_decisions(tmp_path):
    ch, hot, _ = _scenario()
    s = _settings(tmp_path)
    ctx = build(s, rpc_transport=ch.transport(), market_transport=_market(), x_transport=_x_transport([]),
                completer=_muse())
    index_once(ctx)
    Path(s.kill_switch_file).write_text("halt")
    out = trader_once(ctx)
    assert out["decisions"] == [] and "kill file" in out["killed"]


def test_no_muse_means_no_trades(tmp_path):
    ch, hot, _ = _scenario()
    ctx = build(_settings(tmp_path), rpc_transport=ch.transport(), market_transport=_market(),
                x_transport=_x_transport([]))
    index_once(ctx)
    add_source(ctx.conn, SourceIn(kind="news", origin_id="n1", origin_url="https://blog.example/hot",
                                  excerpt=f"contract {hot.token}", confidence="first_party"))
    research_once(ctx)
    d = [x for x in trader_once(ctx)["decisions"] if x["token"] == hot.token][0]
    assert d["action"] == "watch"
    assert rows(ctx.conn, "SELECT COUNT(*) c FROM positions")[0]["c"] == 0


def test_milestone_update_and_exit_thread_under_the_entry(tmp_path):
    ch, hot, _ = _scenario()
    sent: list[dict] = []

    def h(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path == "/2/tweets":
            sent.append(json.loads(req.content))
            return httpx.Response(201, json={"data": {"id": str(1000 + len(sent))}})
        return httpx.Response(404)

    ctx = build(_settings(tmp_path), rpc_transport=ch.transport(), market_transport=_market(),
                x_transport=httpx.MockTransport(h), completer=_muse())
    index_once(ctx)
    add_source(ctx.conn, SourceIn(kind="news", origin_id="n1", origin_url="https://blog.example/hot",
                                  excerpt=f"HOT team ships, contract {hot.token}", confidence="first_party"))
    research_once(ctx)
    trader_once(ctx)
    social_once(ctx)
    assert sent[0]["text"].startswith("PAPER BUY $HOT") and "reply" not in sent[0]

    hot.real_quote = int(5.2 * ETH)  # price up past +25%
    out = trader_once(ctx)
    assert out["exits"][0]["action"] == "hold" and out["exits"][0]["pnl_pct"] >= 25, out["exits"]
    trader_once(ctx)  # same milestone again: no second update
    social_once(ctx)
    assert len(sent) == 2
    assert sent[1]["text"].startswith("$HOT +") and "Still holding" in sent[1]["text"]
    assert "MC (" in sent[1]["text"], sent[1]["text"]
    assert sent[1]["reply"] == {"in_reply_to_tweet_id": "1001"}

    hot.real_quote = int(0.2 * ETH)  # collapse -> stop loss, exit replies under the entry too
    trader_once(ctx)
    social_once(ctx)
    assert sent[-1]["text"].startswith("PAPER SOLD $HOT") and sent[-1]["reply"] == {"in_reply_to_tweet_id": "1001"}
    assert len(sent) == 3


def test_repeat_calls_are_not_reposted(tmp_path):
    from ct_agent.social import compose
    from ct_agent.social.outbox import enqueue
    from ct_agent.strategy.engine import _thread
    ctx = build(_settings(tmp_path), rpc_transport=FakeChain(head=1).transport(), market_transport=_market(),
                x_transport=_x_transport([]))
    tok = addr(0xA1)
    assert _thread(ctx.conn, tok, "watch") == (False, None)
    enqueue(ctx.conn, compose.watch_note(symbol="HOT", token=tok, thesis="t", risk="r", decision_id=1))
    assert _thread(ctx.conn, tok, "watch") == (True, "watch:1")        # same call again: skipped
    assert _thread(ctx.conn, tok, "pass") == (False, "watch:1")        # new call: threads under it
    assert _thread(ctx.conn, tok, "trade_entry") == (False, "watch:1")


def test_ten_trades_per_day_cap(tmp_path):
    from ct_agent.db import utcnow
    ctx = build(_settings(tmp_path, max_open_positions=50, max_total_exposure_eth=10, paper_starting_eth=10),
                rpc_transport=FakeChain(head=1).transport(), market_transport=_market(), x_transport=_x_transport([]))
    for i in range(10):
        ctx.conn.execute("INSERT INTO positions(token,mode,status,quote_symbol,opened_at,entry_quote,entry_tokens,"
                         "entry_price,closed_at,exit_quote,pnl_quote) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         (addr(0xE00 + i), "paper", "closed", "ETH", utcnow(), 0.01, 1, 0.01, utcnow(), 0.01, 0))
    v = ctx.risk.check_entry(token=addr(0xA1), quote_symbol="ETH", mode="paper")
    assert not v.ok and any("10 new trades" in r for r in v.reasons)


def test_waits_for_x_research_before_deciding(tmp_path):
    from ct_agent.db import kv_set
    ch, hot, _ = _scenario()
    muse = _muse()
    ctx = build(_settings(tmp_path, x_bearer_token="app"), rpc_transport=ch.transport(), market_transport=_market(),
                x_transport=_x_transport([]), completer=muse)
    index_once(ctx)
    d = [x for x in trader_once(ctx)["decisions"] if x["token"] == hot.token][0]
    assert d == {"token": hot.token, "skipped": "waiting for X research"}
    kv_set(ctx.conn, f"x_attention:{hot.token.lower()}", {"count_24h": 0, "measured_at": "now"})
    d = [x for x in trader_once(ctx)["decisions"] if x["token"] == hot.token][0]
    assert d["action"] == "pass"  # researched: decided (no sources, so a free pass)


def test_call_outcomes_are_tracked_and_reported(tmp_path):
    from ct_agent.strategy.outcomes import report
    ch, hot, _ = _scenario()
    ctx = build(_settings(tmp_path), rpc_transport=ch.transport(), market_transport=_market(),
                x_transport=_x_transport([]), completer=_muse())
    index_once(ctx)
    trader_once(ctx)
    r = rows(ctx.conn, "SELECT * FROM call_outcomes WHERE token=?", (hot.token,))[0]
    assert r["action"] == "pass" and r["price0"] > 0 and r["venue0"] == "curve"
    trader_once(ctx)  # decided again later: still one row per token per day
    assert rows(ctx.conn, "SELECT COUNT(*) c FROM call_outcomes WHERE token=?", (hot.token,))[0]["c"] == 1

    ctx.conn.execute("UPDATE call_outcomes SET decided_at='2026-01-01T00:00:00+00:00'")  # a day ago and more
    hot.real_quote = int(5.2 * ETH)  # price went up
    out = trader_once(ctx)
    assert out["outcomes"] >= 1
    r = rows(ctx.conn, "SELECT * FROM call_outcomes WHERE token=?", (hot.token,))[0]
    assert r["p15m"] > r["price0"] and r["p24h"] is not None and r["done"] == 1
    rep = report(ctx.conn)
    assert rep["calls_tracked"] >= 1 and rep["overall"]["hit_rate_+25%"] is not None
    assert "curve progress" in rep["by_variable"] and "pass" in rep["by_variable"]["action"]
