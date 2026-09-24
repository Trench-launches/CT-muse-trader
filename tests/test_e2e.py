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
                decision_cooldown_min=0, min_fees_paid_eth=0)  # the thin-chart gate has its own test
    base.update(kw)
    return Settings(**base)


def _muse(action="buy", ids=None):
    calls = []

    def complete(instructions, text, schema, name):
        calls.append(text)
        if name == "exit_review":
            return json.dumps({"action": "hold", "confidence": 0.3, "reasons": ["thesis intact"]})
        if name == "site_chat":
            return json.dumps({"reply": "HOT is on the curve and buyers are spreading out. Not advice."})
        if name == "loss_review":
            return json.dumps({"bought_because": "first-party news and fast buys",
                               "underestimated": "how fast the buyers could leave",
                               "lesson": "wait for buys from more wallets"})
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
    assert len(sent) == 1 and sent[0].startswith("PAPER BUY $HOT") and hot.token not in sent[0]  # ticker only
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
    assert sent[-1].startswith("Top blasted $HOT on paper. Entry was poor.") and "Realised P&L: -" in sent[-1], sent[-1]
    assert "Underestimated how fast the buyers could leave." in sent[-1] and "Lesson: wait for buys from more wallets." in sent[-1]
    assert journal_recent(ctx.conn, kind="lesson")[0]["payload"]["lesson"] == "wait for buys from more wallets"
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

    # the website's one-call payload: real figures from the same database
    from ct_agent.api import site as site_mod
    site_mod._CACHE.clear()
    r = api.get("/site", headers={"origin": "https://www.mooseonmuse.com"})
    assert r.headers["access-control-allow-origin"] == "https://www.mooseonmuse.com"
    d = r.json()
    assert d["score"]["closed"] == 1 and d["score"]["losses"] == 1 and len(d["score"]["series"]) == 2
    sc = d["score"]
    assert sc["taken"] == 1 and sc["win_rate"] == 0 and sc["net_eth"] < 0 and sc["return_pct"] < 0
    assert sc["worst"]["symbol"] == "HOT" and sc["recent"][0]["symbol"] == "HOT" and sc["labels"] == ["HOT"]
    assert sc["avg_hold_h"] is not None and sc["recent"][0]["reason"]
    assert "logo" in sc["recent"][0]
    assert d["decisions"] and d["decisions"][0]["symbol"] == "HOT" and d["decisions"][0]["evidence"]
    assert d["posts"][0]["body"].startswith("Top blasted $HOT") and d["kill_switch"]["on"] is True
    assert d["funnel"]["traded_24h"] == 1 and d["sources"] and d["chain"]["journal_intact"] is True
    assert "access-control-allow-origin" not in api.get("/site", headers={"origin": "https://evil.example"}).headers


def test_rumor_only_cannot_trigger_buy(tmp_path):
    ch, hot, _ = _scenario()
    ctx = build(_settings(tmp_path, min_independent_sources_for_buy=1), rpc_transport=ch.transport(), market_transport=_market(),
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
    ctx = build(_settings(tmp_path, min_independent_sources_for_buy=1), rpc_transport=ch.transport(), market_transport=_market(),
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
    assert sent[-1]["text"].startswith("Top blasted $HOT") and sent[-1]["reply"] == {"in_reply_to_tweet_id": "1001"}
    assert len(sent) == 3


def test_repeat_calls_are_not_reposted(tmp_path):
    from ct_agent.social import compose
    from ct_agent.social.outbox import enqueue
    from ct_agent.strategy.engine import _thread
    ctx = build(_settings(tmp_path), rpc_transport=FakeChain(head=1).transport(), market_transport=_market(),
                x_transport=_x_transport([]))
    tok = addr(0xA1)
    assert _thread(ctx.conn, tok, "watch") == (False, None)
    enqueue(ctx.conn, compose.watch_note(symbol="HOT", token=tok, line="t", decision_id=1))
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
    ctx = build(_settings(tmp_path, x_bearer_token="app", min_independent_sources_for_buy=1), rpc_transport=ch.transport(), market_transport=_market(),
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
    ctx = build(_settings(tmp_path, min_independent_sources_for_buy=1), rpc_transport=ch.transport(), market_transport=_market(),
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


def _entered(tmp_path, **kw):
    ch, hot, _ = _scenario()
    sent: list[str] = []
    ctx = build(_settings(tmp_path, **kw), rpc_transport=ch.transport(), market_transport=_market(),
                x_transport=_x_transport(sent), completer=_muse())
    index_once(ctx)
    add_source(ctx.conn, SourceIn(kind="news", origin_id="n1", origin_url="https://blog.example/hot",
                                  excerpt=f"HOT team ships, contract {hot.token}", confidence="first_party"))
    research_once(ctx)
    trader_once(ctx)
    social_once(ctx)
    base = 1.68 * ETH + hot.real_quote  # curve price scales with (phantom + real reserve)^2
    at = lambda m: int(base * m ** 0.5 - 1.68 * ETH)  # noqa: E731
    return ctx, hot, sent, at


def test_take_profit_ladder_then_breakeven_stop(tmp_path):
    ctx, hot, sent, at = _entered(tmp_path)
    pos = lambda: rows(ctx.conn, "SELECT * FROM positions")[0]  # noqa: E731
    entry_tokens = pos()["entry_tokens"]

    hot.real_quote = at(1.75)
    ex = trader_once(ctx)["exits"][0]
    assert ex["action"] == "take_profit" and ex["rung"] == 1, ex
    p = pos()
    assert p["status"] == "open" and p["tp_hits"] == 1 and p["tokens_left"] == pytest.approx(entry_tokens * 0.5)
    social_once(ctx)
    assert sent[-1].startswith("Jeeted 50% of my $HOT paper bag at $") and "stop moved to entry" in sent[-1], sent[-1]
    assert "50% still riding. Realised P&L: +" in sent[-1]

    hot.real_quote = at(1.3)  # below the next rung, above entry: hold
    assert trader_once(ctx)["exits"][0]["action"] == "hold"

    hot.real_quote = at(2.3)
    ex = trader_once(ctx)["exits"][0]
    assert ex["action"] == "take_profit" and ex["rung"] == 2, ex
    assert pos()["tokens_left"] == pytest.approx(entry_tokens * 0.25)

    hot.real_quote = at(0.97)  # runner falls back under entry: breakeven stop sells the rest
    ex = trader_once(ctx)["exits"][0]
    assert ex["action"] == "exit" and "back to entry" in ex["reason"], ex
    social_once(ctx)
    out = [t for t in sent if t.startswith("Jeeted the last 25% of my $HOT paper bag")]
    assert len(out) == 1 and "Fully out. Realised P&L: +" in out[0], sent
    p = pos()
    assert p["status"] == "closed" and p["pnl_quote"] > 0 and p["exit_quote"] > p["realized_quote"]
    assert p["pnl_quote"] == pytest.approx(p["exit_quote"] - p["entry_quote"])
    assert journal_verify(ctx.conn) == (True, None)


def test_jump_past_every_rung_sells_them_and_the_rest_rides(tmp_path):
    ctx, hot, sent, at = _entered(tmp_path)
    hot.real_quote = at(9)
    ex = trader_once(ctx)["exits"][0]
    assert ex["action"] == "take_profit" and ex["rung"] == 2, ex  # 50% + 25% in one go
    p = rows(ctx.conn, "SELECT * FROM positions")[0]
    assert p["status"] == "open" and p["tokens_left"] == pytest.approx(p["entry_tokens"] * 0.25)
    assert trader_once(ctx)["exits"][0]["action"] == "hold"  # no rung left: the runner rides


def test_timer_sells_whatever_is_left(tmp_path):
    ctx, hot, sent, at = _entered(tmp_path, max_hold_hours=1.0)
    hot.real_quote = at(1.75)
    assert trader_once(ctx)["exits"][0]["action"] == "take_profit"
    ctx.conn.execute("UPDATE positions SET opened_at='2026-01-01T00:00:00+00:00'")  # held for ages
    ex = trader_once(ctx)["exits"][0]
    assert ex["action"] == "exit" and "max hold" in ex["reason"], ex
    assert rows(ctx.conn, "SELECT status FROM positions")[0]["status"] == "closed"


def test_thesis_review_never_sells(tmp_path):
    ctx, hot, sent, at = _entered(tmp_path)
    ctx.brain.review_exit = lambda pack: (_ for _ in ()).throw(AssertionError("exits are fixed rules now"))
    hot.real_quote = at(0.8)  # a fresh position that looks down (fees and slippage): no panic sell
    assert trader_once(ctx)["exits"][0]["action"] == "hold"
    hot.real_quote = at(1.75)
    trader_once(ctx)
    hot.real_quote = at(1.4)
    assert trader_once(ctx)["exits"][0]["action"] == "hold"


def test_buys_on_onchain_activity_without_a_source(tmp_path):
    ch, hot, _ = _scenario()
    sent: list[str] = []
    muse = _muse(ids=[])
    ctx = build(_settings(tmp_path), rpc_transport=ch.transport(), market_transport=_market(),
                x_transport=_x_transport(sent), completer=muse)
    index_once(ctx)
    d = [x for x in trader_once(ctx)["decisions"] if x["token"] == hot.token][0]
    assert muse.calls and '"verified_source_required": false' in muse.calls[0]  # asked with no source at all
    assert d["action"] == "buy" and d.get("entered") and not d["gates"], d
    assert rows(ctx.conn, "SELECT entry_quote FROM positions")[0]["entry_quote"] == pytest.approx(0.05)
    social_once(ctx)
    assert sent[0].startswith("PAPER BUY $HOT") and "Saw broad onchain buying" in sent[0], sent[0]


def _chat_api(tmp_path, **kw):
    ch, hot, cold = _scenario()
    muse = _muse()
    ctx = build(_settings(tmp_path, **kw), rpc_transport=ch.transport(), market_transport=_market(),
                x_transport=_x_transport([]), completer=muse)
    index_once(ctx)
    app = create_app(ctx.settings)
    app.state.chat_services = {"rpc": ctx.rpc, "pons": ctx.pons, "market": ctx.market, "brain": ctx.brain}
    return TestClient(app), hot, muse


def test_ask_muse_researches_a_contract(tmp_path):
    from ct_agent.api import chat
    chat._RESEARCH.clear()
    api, hot, muse = _chat_api(tmp_path)
    # a suggestion chip sends the coin's address, so it resolves even before its ticker is indexed
    d = api.post("/chat", json={"message": "Research $HOT", "token": hot.token}).json()
    assert d["token"]["symbol"] == "HOT", d
    chat._RESEARCH.clear()
    r = api.post("/chat", json={"message": f"what do you think of {hot.token}?"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["reply"].startswith("HOT is on the curve") and d["remaining"]["hour"] == 8
    assert d["token"]["symbol"] == "HOT" and d["token"]["stage"] == "on the bonding curve"
    sent = muse.calls[-1]
    assert '"curve_progress_pct"' in sent and '"onchain_activity"' in sent and "<visitor_conversation>" in sent
    assert "visitor: what do you think of" in sent

    d = api.post("/chat", json={"message": "is $hot risky?"}).json()  # ticker lookup, any case
    assert d["token"]["address"].lower() == hot.token.lower()

    # a follow-up with no coin named uses the one from earlier in the conversation
    d = api.post("/chat", json={"message": "and the fees?", "history": [
        {"role": "user", "text": f"look at {hot.token}"}, {"role": "muse", "text": "ok"}]}).json()
    assert d["token"]["symbol"] == "HOT"

    d = api.post("/chat", json={"message": f"check {addr(0xDEAD)}"}).json()  # not a Pons launch
    assert d["token"] is None and '"is_pons_v2_launch_on_robinhood_chain": false' in muse.calls[-1]

    assert api.post("/chat", json={"message": "x" * 501}).status_code == 400
    pre = api.options("/chat", headers={"origin": "https://www.mooseonmuse.com", "access-control-request-method": "POST",
                                        "access-control-request-headers": "content-type"})
    assert pre.status_code == 200 and pre.headers["access-control-allow-origin"] == "https://www.mooseonmuse.com"
    pre = api.options("/chat", headers={"origin": "https://www.mooseonmuse.com", "access-control-request-method": "POST",
                                        "access-control-request-headers": "authorization"})
    assert pre.status_code == 400  # no Authorization header from browsers: admin calls stay server-side


def test_ask_muse_is_rate_limited(tmp_path):
    api, hot, muse = _chat_api(tmp_path, chat_per_ip_hour=2, chat_global_day=3)
    one = {"x-forwarded-for": "1.1.1.1"}
    assert api.get("/chat/limits", headers=one).json()["remaining"] == {"hour": 2, "day": 30}
    for _ in range(2):
        assert api.post("/chat", json={"message": "hi"}, headers=one).status_code == 200
    r = api.post("/chat", json={"message": "hi"}, headers=one)
    assert r.status_code == 429 and "questions this hour" in r.json()["detail"] and int(r.headers["retry-after"]) >= 60
    # a spoofed first X-Forwarded-For entry doesn't reset the limit: the proxy's last entry is used
    assert api.post("/chat", json={"message": "hi"}, headers={"x-forwarded-for": "9.9.9.9, 1.1.1.1"}).status_code == 429
    # another visitor still gets through until the site-wide daily cap
    assert api.post("/chat", json={"message": "hi"}, headers={"x-forwarded-for": "2.2.2.2"}).status_code == 200
    r = api.post("/chat", json={"message": "hi"}, headers={"x-forwarded-for": "3.3.3.3"})
    assert r.status_code == 429 and "all it can for today" in r.json()["detail"]
    calls = len(muse.calls)
    assert calls == 3  # refused requests never reached Muse


def test_muse_reads_the_coins_own_lore(tmp_path):
    from ct_agent.api import chat
    ch, hot, _ = _scenario()
    hot.description = "HOT is the cat who guards every fridge on Robinhood Chain."
    hot.socials = "https://x.com/hotcoin"
    muse = _muse()
    ctx = build(_settings(tmp_path), rpc_transport=ch.transport(), market_transport=_market(),
                x_transport=_x_transport([]), completer=muse)
    index_once(ctx)
    add_source(ctx.conn, SourceIn(kind="news", origin_id="n1", origin_url="https://blog.example/hot",
                                  excerpt=f"HOT ships, contract {hot.token}", confidence="first_party"))
    research_once(ctx)
    trader_once(ctx)
    sent = muse.calls[0]
    trusted, untrusted = sent.split("<untrusted_evidence>")
    assert "guards every fridge" in untrusted and "COIN'S OWN LORE" in untrusted
    assert "guards every fridge" not in trusted  # creator-written text never sits with the trusted numbers
    assert '"x": "hotcoin"' in untrusted

    chat._RESEARCH.clear()
    app = create_app(ctx.settings)
    app.state.chat_services = {"rpc": ctx.rpc, "pons": ctx.pons, "market": ctx.market, "brain": ctx.brain}
    TestClient(app).post("/chat", json={"message": f"what about {hot.token}"})
    assert "guards every fridge" in muse.calls[-1].split("<untrusted_evidence>")[1]


def test_chat_streams_each_research_step(tmp_path):
    from ct_agent.api import chat
    chat._RESEARCH.clear()
    api, hot, muse = _chat_api(tmp_path)
    hot.description = "HOT is the cat who guards every fridge on Robinhood Chain."
    hot.socials = "https://x.com/hotcoin"
    r = api.post("/chat/stream", json={"message": f"look at {hot.token}"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/x-ndjson")
    ev = [json.loads(line) for line in r.text.splitlines() if line.strip()]
    done = [(e["id"], e["status"]) for e in ev if e["type"] == "step" and e["status"] != "run"]
    assert done == [("coin", "done"), ("onchain", "done"), ("lore", "done"), ("x", "skip"), ("website", "skip"),
                    ("muse", "done")], done
    steps = {e["id"]: e["detail"] for e in ev if e["type"] == "step" and e["status"] != "run"}
    assert steps["coin"] == "$HOT · Hot" and "curve" in steps["onchain"] and "guards every fridge" in steps["lore"]
    assert steps["x"] == "@hotcoin (profile not checked)"  # no X token in tests: shown, not faked
    assert ev[-1]["type"] == "answer" and ev[-1]["reply"].startswith("HOT is on the curve")
    # a general question has no research card: straight to Muse
    ev = [json.loads(line) for line in api.post("/chat/stream", json={"message": "how does MOOSE pick trades?"}).text.splitlines()]
    assert [e["id"] for e in ev if e["type"] == "step"] == ["muse", "muse"] and ev[-1]["type"] == "answer"
    # limits still apply before anything streams
    assert api.post("/chat/stream", json={"message": "x" * 501}).status_code == 400


def test_thin_chart_gate_blocks_a_buy(tmp_path):
    ch, hot, _ = _scenario()  # ~$52K market cap, but only 11 small curve trades (~0.005 ETH of fees)
    ctx = build(_settings(tmp_path, min_fees_paid_eth=0.02), rpc_transport=ch.transport(), market_transport=_market(),
                x_transport=_x_transport([]), completer=_muse())
    index_once(ctx)
    add_source(ctx.conn, SourceIn(kind="news", origin_id="n1", origin_url="https://blog.example/hot",
                                  excerpt=f"HOT ships, contract {hot.token}", confidence="first_party"))
    research_once(ctx)
    d = [x for x in trader_once(ctx)["decisions"] if x["token"] == hot.token][0]
    assert d["action"] == "watch" and any("trading fees paid" in g for g in d["gates"]), d
    assert rows(ctx.conn, "SELECT COUNT(*) c FROM positions")[0]["c"] == 0
    from ct_agent.strategy.engine import fees_paid_eth
    launch = rows(ctx.conn, "SELECT * FROM launches WHERE token=?", (hot.token,))[0]
    assert fees_paid_eth(ctx, launch) == pytest.approx(11 * 0.05 * 0.01, rel=0.3)  # summed from the trade events
    # plenty of fees paid: the same coin passes the gate
    ctx2 = build(_settings(tmp_path / "b", min_fees_paid_eth=0.001), rpc_transport=ch.transport(),
                 market_transport=_market(), x_transport=_x_transport([]), completer=_muse())
    index_once(ctx2)
    add_source(ctx2.conn, SourceIn(kind="news", origin_id="n1", origin_url="https://blog.example/hot",
                                   excerpt=f"HOT ships, contract {hot.token}", confidence="first_party"))
    research_once(ctx2)
    assert [x for x in trader_once(ctx2)["decisions"] if x["token"] == hot.token][0]["action"] == "buy"


def test_holder_gate_blocks_a_buy(tmp_path):
    from ct_agent.strategy.engine import holder_facts
    ch, hot, _ = _scenario()
    ch.trade(hot, ch.head - 2_000, "buy", int(0.05 * ETH), 4 * 10**26, addr(0xBADBAD))  # one wallet takes 40% of supply
    ctx = build(_settings(tmp_path), rpc_transport=ch.transport(), market_transport=_market(),
                x_transport=_x_transport([]), completer=_muse())
    index_once(ctx)
    add_source(ctx.conn, SourceIn(kind="news", origin_id="n1", origin_url="https://blog.example/hot",
                                  excerpt=f"HOT ships, contract {hot.token}", confidence="first_party"))
    research_once(ctx)
    d = [x for x in trader_once(ctx)["decisions"] if x["token"] == hot.token][0]
    assert d["action"] == "watch" and any("10 biggest wallets hold" in g for g in d["gates"]), d
    launch = rows(ctx.conn, "SELECT * FROM launches WHERE token=?", (hot.token,))[0]
    hs = holder_facts(ctx, launch)
    assert hs["top_wallet_share"] == pytest.approx(0.4) and hs["holders"] == 12  # the curve itself isn't counted
    assert hs["deployer_share"] == 0


def test_rug_guards_fail_safe_when_the_chain_cant_be_read(tmp_path):
    ch, hot, _ = _scenario()
    ctx = build(_settings(tmp_path, min_fees_paid_eth=0.001), rpc_transport=ch.transport(), market_transport=_market(),
                x_transport=_x_transport([]), completer=_muse())
    index_once(ctx)
    add_source(ctx.conn, SourceIn(kind="news", origin_id="n1", origin_url="https://blog.example/hot",
                                  excerpt=f"HOT ships, contract {hot.token}", confidence="first_party"))
    research_once(ctx)

    def rate_limited(*a, **k):
        raise RuntimeError("429 too many requests")
    ctx.pons.token_transfers = rate_limited
    ctx.pons.curve_fees = rate_limited
    d = [x for x in trader_once(ctx)["decisions"] if x["token"] == hot.token][0]
    assert d["action"] == "watch", d
    assert "couldn't check who holds the supply" in d["gates"] and "couldn't check the fees traders have paid" in d["gates"]
