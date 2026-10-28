"""End-to-end: fake Solana + fake Jupiter + fake scanner + fake Grok + fake X, real code in between.

scanner call -> tradeable check (mint safety, Jupiter route) -> Grok's read -> +30% rule buy (paper fill from a
Jupiter quote) -> exits (stop, take-profit ladder, timer) -> posts -> API reads it all back.
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from ct_agent.api.app import create_app
from ct_agent.config import Settings
from ct_agent.context import build
from ct_agent.db import journal_recent, journal_verify, rows
from ct_agent.workers.runner import exits_once, social_once, trader_once

from .fakes import Coin, FakeJupiter, FakeSolana, market_transport, mint, wallet

CALL_PRICE = 0.0001


def _call(i, coin, hours_ago=1.0, price=CALL_PRICE):
    ts = int((datetime.now(timezone.utc) - timedelta(hours=hours_ago)).timestamp() * 1000)
    return {"id": i, "token_address": coin.mint, "token_symbol": coin.symbol, "alert_price": price,
            "alert_mcap": 100_000, "alert_timestamp": ts, "alert_signals": json.dumps({"positive": ["SM net buy $5k"]}),
            "alert_liquidity": 50_000, "source": "nansen_screener"}


def _hot(**kw) -> Coin:
    base = dict(symbol="HOT", name="Hot", price_usd=CALL_PRICE * 1.35, liquidity_usd=6_000_000,
                holders=[(wallet(i), 10**13, False) for i in range(6)] + [(wallet(99), 2 * 10**14, True)])
    base.update(kw)
    return Coin(mint(1), **base)


def _settings(tmp_path, **kw) -> Settings:
    base = dict(_env_file=None, database_path=str(tmp_path / "e2e.db"), kill_switch_file=str(tmp_path / "KILL"),
                x_posting_enabled=True, x_user_access_token="user-token", api_admin_token="admin-secret",
                supabase_url="https://scan.supabase.co", supabase_key="k", jupiter_api_key="test",
                chat_enabled=True, site_origins=["https://site.example", "https://www.site.example"])
    base.update(kw)
    return Settings(**base)


def _grok(action="watch"):
    calls = []

    def complete(instructions, text, schema, name):
        calls.append(text)
        if name == "site_chat":
            return json.dumps({"reply": "HOT is a cat coin in a PumpSwap pool and buyers are spreading out. Not advice."})
        if name == "thesis_update":
            return json.dumps({"thesis": "buyers kept spreading out after the call, no big wallet selling yet"})
        if name == "loss_review":
            return json.dumps({"bought_because": "a smart-money call that kept running",
                               "underestimated": "how fast the buyers could leave",
                               "lesson": "wait for buys from more wallets"})
        return json.dumps({"action": action, "confidence": 0.72, "thesis": "smart money in and the cat meme is fresh",
                           "reasons": ["smart-money call", "buyers spreading out"],
                           "risks": ["one-hour spike may fade"], "evidence_ids": [],
                           "underfollowed": None, "post_angle": "call",
                           "public_line": "Smart money is in and the buying is spreading out.",
                           "public_thesis": "HOT is the cat that guards the fridge, and cat coins are running.",
                           "public_reasons": ["six wallets bought, none holds more than 1%"],
                           "public_risk": "a one-hour spike can fade fast",
                           "post": {"narrative": "cat meme", "confirmation": "more buyers", "watching": "the run",
                                    "trigger": "", "concern": "", "reconsider_if": ""}})
    complete.calls = calls
    return complete


def _x_transport(sent: list, full: bool = False):
    def h(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path == "/2/tweets":
            body = json.loads(req.content)
            sent.append(body if full else body["text"])
            return httpx.Response(201, json={"data": {"id": str(1000 + len(sent)), "text": body["text"]}})
        return httpx.Response(404)
    return httpx.MockTransport(h)


def _world(coin=None, calls=None):
    ch = FakeSolana([coin or _hot()])
    jup = FakeJupiter(ch)
    return ch, jup


def _ctx(tmp_path, ch, jup, calls, sent=None, grok=None, full=False, **kw):
    return build(_settings(tmp_path, **kw), rpc_transport=ch.transport(),
                 market_transport=market_transport(ch, supabase=calls), jupiter_transport=jup.transport(),
                 x_transport=_x_transport(sent if sent is not None else [], full), completer=grok or _grok())


def test_full_paper_cycle(tmp_path):
    ch, jup = _world()
    hot = ch.coins[mint(1)]
    sent: list[str] = []
    grok = _grok()
    ctx = _ctx(tmp_path, ch, jup, [_call(1, hot)], sent, grok)

    out = trader_once(ctx)
    buy = [d for d in out["decisions"] if d.get("action") == "buy"]
    assert buy and buy[0]["entered"], out
    assert "<untrusted_evidence>" in grok.calls[0] and '"chain": "Solana"' in grok.calls[0]
    assert '"mint_safety"' in grok.calls[0] and '"top10_share": 0.06' in grok.calls[0]  # the pool's 20% isn't a holder

    pos = rows(ctx.conn, "SELECT * FROM positions")
    assert len(pos) == 1 and pos[0]["mode"] == "paper" and pos[0]["quote_symbol"] == "SOL"
    assert pos[0]["entry_quote"] == pytest.approx(0.5) and pos[0]["token"] == hot.mint  # never lowercased
    fill = journal_recent(ctx.conn, kind="paper_fill")[0]["payload"]
    assert fill["onchain"] is False and fill["pricing"] == "jupiter_quote"
    # tokens = 0.5 SOL at the coin's price, less Jupiter's 0.1% impact, in the coin's own 6 decimals
    assert pos[0]["entry_tokens"] == pytest.approx(0.5 * 150 / hot.price_usd * 0.999, rel=1e-4)
    coin = rows(ctx.conn, "SELECT * FROM tokens WHERE mint=?", (hot.mint,))[0]
    assert (coin["symbol"], coin["decimals"], coin["stage"], coin["dex_id"]) == ("HOT", 6, "pool", "pumpswap")

    social_once(ctx)
    assert sent[0].startswith("👀 $HOT is on my radar")                     # Grok's read on the call
    assert sent[1].startswith("🟢 Test buy $HOT") and hot.mint not in sent[1]  # ticker only
    assert "\nPlan: -70% stop" in sent[1] and "Size: 0.5000 SOL" in sent[1] and len(sent[1]) <= 280
    assert "\nEntry: $" in sent[1] and " MC\n" in sent[1] and "smart-money call on Solana" in sent[1], sent[1]

    # second cycle: position held, no duplicate entry or post
    out2 = trader_once(ctx)
    assert out2["exits"][0]["action"] == "hold"
    assert rows(ctx.conn, "SELECT COUNT(*) c FROM positions")[0]["c"] == 1

    # price collapses -> stop loss -> exit posted, loss logged
    hot.price_usd = CALL_PRICE * 0.3
    calls = len(grok.calls)
    jup.calls.clear()
    out3 = exits_once(ctx)
    assert out3["exits"][0]["action"] == "exit" and "stop loss" in out3["exits"][0]["reason"]
    assert jup.calls == ["/price/v3", "/swap/v2/order"]  # one batched mark, then a real quote for the sale
    assert len(grok.calls) == calls and journal_recent(ctx.conn, kind="post_job")  # the sell didn't wait on Grok
    social_once(ctx)
    assert len(grok.calls) > calls  # the post-mortem is written in the posting loop
    assert sent[-1].startswith("🔴 Top blasted $HOT (testing) · -") and "\nRealised P&L: -" in sent[-1], sent[-1]
    assert " SOL (-" in sent[-1] and "Lesson: wait for buys from more wallets." in sent[-1]
    assert journal_verify(ctx.conn) == (True, None)

    # API reads it back; admin needs the token
    api = TestClient(create_app(ctx.settings))
    h = api.get("/health").json()
    assert h["mode"] == "paper" and h["journal"]["chain_intact"] and h["counts"]["tokens"] == 1
    sc = api.get("/scorecard").json()
    assert sc["losses"] == 1 and sc["net_pnl_sol"] < 0 and "paper_cash_sol" in sc
    assert api.get(f"/tokens/{hot.mint}").json()["positions"][0]["status"] == "closed"
    assert api.get(f"/tokens/{hot.mint.lower()}").status_code == 404  # base58 is case-sensitive
    assert api.post("/admin/kill", json={"on": True, "reason": "test stop"}).status_code == 401
    assert api.post("/admin/kill", json={"on": True, "reason": "test stop"},
                    headers={"authorization": "Bearer admin-secret"}).status_code == 200
    assert api.get("/health").json()["kill_switch"]["on"] is True

    # the website's one-call payload: real figures from the same database
    from ct_agent.api import site as site_mod
    site_mod._CACHE.clear()
    r = api.get("/site", headers={"origin": "https://site.example"})
    assert r.headers["access-control-allow-origin"] == "https://site.example"
    assert "access-control-allow-origin" not in api.get("/site", headers={"origin": "https://evil.example"}).headers
    d = r.json()
    sc = d["score"]
    assert sc["closed"] == 1 and sc["losses"] == 1 and len(sc["series"]) == 2 and sc["net_sol"] < 0
    assert sc["worst"]["symbol"] == "HOT" and sc["worst"]["pnl_sol"] < 0 and sc["recent"][0]["logo"] == f"/logo/{hot.mint}"
    assert {"deployed_sol", "avg_sol", "unrealized_sol"} <= set(sc) and not any(k.endswith("_eth") for k in sc)
    assert d["funnel"] == {"calls_24h": 1, "screened_24h": 1, "bought_24h": 1}
    w = d["watching"][0]
    assert (w["token"], w["status"], w["stage"], w["dex"]) == (hot.mint, "bought", "pool", "pumpswap")
    assert "progress" not in w and {"move_pct", "called_at", "left_h", "note"} <= set(w)
    assert d["decisions"] and d["decisions"][0]["stage"] == "pool" and d["decisions"][0]["dex"] == "pumpswap"
    assert d["chain"]["journal_intact"] is True and "block" not in d["chain"]
    assert d["posts"][0]["body"].startswith("🔴 Top blasted $HOT") and d["kill_switch"]["on"] is True


def test_untradeable_calls_are_skipped_in_plain_words_but_still_read(tmp_path):
    ch, jup = _world()
    risky = ch.add(Coin(mint(2), symbol="FRZ", price_usd=CALL_PRICE * 2, freeze_authority=wallet(7)))
    stuck = ch.add(Coin(mint(3), symbol="NOPE", price_usd=CALL_PRICE * 2, routable=False))
    taxed = ch.add(Coin(mint(4), symbol="TAX", price_usd=CALL_PRICE * 2, program="TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
                        extensions=[{"extension": "transferFeeConfig", "state": {"newerTransferFee": {
                            "transferFeeBasisPoints": 500}}}]))
    evm = {"id": 5, "token_address": "0x" + "ab" * 20, "token_symbol": "EVM", "alert_price": 1.0, "alert_mcap": 1,
           "alert_timestamp": int(datetime.now(timezone.utc).timestamp() * 1000), "source": "x"}
    grok = _grok()
    ctx = _ctx(tmp_path, ch, jup, [_call(2, risky), _call(3, stuck), _call(4, taxed), evm], grok=grok)
    trader_once(ctx)
    st = {r["token"]: r for r in rows(ctx.conn, "SELECT token, status, note FROM scanner_calls")}
    assert st[risky.mint]["status"] == "skipped" and "freeze" in st[risky.mint]["note"]
    assert "can't route" in st[stuck.mint]["note"] and "5% tax on every transfer" in st[taxed.mint]["note"]
    assert "not a Solana token" in st["0x" + "ab" * 20]["note"]
    assert rows(ctx.conn, "SELECT COUNT(*) c FROM positions")[0]["c"] == 0
    assert any('"tradeable_by_vigil": false' in c for c in grok.calls)  # untradeable calls still get Grok's read


def test_buy_on_call_buys_first_and_reads_under_it(tmp_path):
    from ct_agent.workers.runner import fast_caller_once
    hot = _hot(price_usd=CALL_PRICE)  # hasn't moved: buy-on-call doesn't wait for +30%
    ch, jup = _world(hot)
    sent: list[dict] = []
    ctx = _ctx(tmp_path, ch, jup, [_call(1, hot)], sent, full=True, scanner_buy_on_call=True)
    assert fast_caller_once(ctx)["bought"][0]["entered"]
    trader_once(ctx)  # the read follows in the trader loop
    social_once(ctx)
    assert sent[0]["text"].startswith("🟢 Test buy $HOT") and sent[1]["text"].startswith("🧠 Why I bought $HOT")
    assert sent[1]["reply"] == {"in_reply_to_tweet_id": "1001"}


def test_kill_switch_stops_new_decisions(tmp_path):
    ch, jup = _world()
    s = _settings(tmp_path)
    ctx = _ctx(tmp_path, ch, jup, [_call(1, ch.coins[mint(1)])])
    Path(s.kill_switch_file).write_text("halt")
    out = trader_once(ctx)
    assert out["decisions"] == [] and "kill file" in out["killed"]
    assert rows(ctx.conn, "SELECT COUNT(*) c FROM positions")[0]["c"] == 0


def test_price_impact_bound_holds_a_paper_buy(tmp_path):
    ch, jup = _world(_hot(impact_pct=5.0))  # 500 bps > the 300 bps bound
    ctx = _ctx(tmp_path, ch, jup, [_call(1, ch.coins[mint(1)])])
    out = trader_once(ctx)
    held = [d for d in out["decisions"] if d.get("action") == "held"]
    assert held and any("price impact 500 bps" in g for g in held[0]["gates"])
    assert rows(ctx.conn, "SELECT COUNT(*) c FROM positions")[0]["c"] == 0


def test_milestone_update_and_exit_thread_under_the_entry(tmp_path):
    ch, jup = _world()
    hot = ch.coins[mint(1)]
    sent: list[dict] = []
    ctx = _ctx(tmp_path, ch, jup, [_call(1, hot)], sent, full=True, x_update_step_pct=25)
    trader_once(ctx)
    social_once(ctx)
    entry = next(p for p in sent if p["text"].startswith("🟢 Test buy $HOT"))
    n = len(sent)
    base = hot.price_usd
    hot.price_usd = base * 1.32  # up past +25%, below the first rung (1.5x)
    out = exits_once(ctx)
    assert out["exits"][0]["action"] == "hold" and out["exits"][0]["pnl_pct"] >= 25, out["exits"]
    exits_once(ctx)  # same milestone again: no second update
    social_once(ctx)
    upd = sent[n:]
    assert len(upd) == 1 and upd[0]["text"].startswith("📈 $HOT +") and "\n\nMC now: $" in upd[0]["text"], upd
    assert "\n\nStill holding: buyers kept spreading out" in upd[0]["text"] and len(upd[0]["text"]) <= 280
    entry_id = str(1000 + sent.index(entry) + 1)
    assert upd[0]["quote_tweet_id"] == entry_id and "reply" not in upd[0]

    hot.price_usd = base * 0.2  # collapse -> stop loss, the exit quotes the entry too
    exits_once(ctx)
    social_once(ctx)
    assert sent[-1]["text"].startswith("🔴 Top blasted $HOT") and sent[-1]["quote_tweet_id"] == entry_id


def _entered(tmp_path, **kw):
    ch, jup = _world()
    hot = ch.coins[mint(1)]
    sent: list[str] = []
    ctx = _ctx(tmp_path, ch, jup, [_call(1, hot)], sent, **kw)
    trader_once(ctx)
    social_once(ctx)
    base = hot.price_usd
    at = lambda m: base * m * 1.012  # noqa: E731  (the mark nets out the pool fee and the entry's impact)
    return ctx, hot, sent, at


def test_take_profit_ladder_then_breakeven_stop(tmp_path):
    ctx, hot, sent, at = _entered(tmp_path)
    pos = lambda: rows(ctx.conn, "SELECT * FROM positions")[0]  # noqa: E731
    entry_tokens = pos()["entry_tokens"]

    hot.price_usd = at(1.6)
    ex = exits_once(ctx)["exits"][0]
    assert ex["action"] == "take_profit" and ex["rung"] == 1, ex
    p = pos()
    assert p["status"] == "open" and p["tp_hits"] == 1 and p["tokens_left"] == pytest.approx(entry_tokens * 0.5, rel=1e-6)
    social_once(ctx)
    assert sent[-1].startswith("💰 Jeeted 50% of my $HOT test bag\n\nSold at: $") and "stop moved to entry" in sent[-1]
    assert "\nRealised: +" in sent[-1] and " SOL\n" in sent[-1] and "\nStill riding: 50%" in sent[-1]
    assert "\n\nWhy I'm holding the rest: buyers kept spreading out" in sent[-1] and len(sent[-1]) <= 280, sent[-1]
    n = len(sent)

    hot.price_usd = at(1.3)  # below the next rung, above entry: hold
    assert exits_once(ctx)["exits"][0]["action"] == "hold"
    social_once(ctx)
    assert len(sent) == n  # the take-profit post already covered the gain: no separate update after it

    hot.price_usd = at(2.2)
    ex = exits_once(ctx)["exits"][0]
    assert ex["action"] == "take_profit" and ex["rung"] == 2, ex
    assert pos()["tokens_left"] == pytest.approx(entry_tokens * 0.25, rel=1e-6)

    hot.price_usd = at(0.97)  # runner falls back under entry: breakeven stop sells the rest
    ex = exits_once(ctx)["exits"][0]
    assert ex["action"] == "exit" and "back to entry" in ex["reason"], ex
    social_once(ctx)
    out = [t for t in sent if t.startswith("✅ Jeeted the rest of $HOT (testing)")]
    assert len(out) == 1 and "\nRealised P&L: +" in out[0] and out[0].endswith("\n\nFully out."), sent
    p = pos()
    assert p["status"] == "closed" and p["pnl_quote"] > 0 and p["exit_quote"] > p["realized_quote"]
    assert p["pnl_quote"] == pytest.approx(p["exit_quote"] - p["entry_quote"])
    assert journal_verify(ctx.conn) == (True, None)


def test_jump_past_every_rung_sells_them_and_the_rest_rides(tmp_path):
    ctx, hot, sent, at = _entered(tmp_path)
    hot.price_usd = at(9)
    ex = exits_once(ctx)["exits"][0]
    assert ex["action"] == "take_profit" and ex["rung"] == 2, ex  # 50% + 25% in one go
    p = rows(ctx.conn, "SELECT * FROM positions")[0]
    assert p["status"] == "open" and p["tokens_left"] == pytest.approx(p["entry_tokens"] * 0.25, rel=1e-6)
    assert exits_once(ctx)["exits"][0]["action"] == "hold"  # no rung left: the runner rides


def test_timer_sells_whatever_is_left(tmp_path):
    ctx, hot, sent, at = _entered(tmp_path, max_hold_hours=1.0)
    hot.price_usd = at(1.6)
    assert exits_once(ctx)["exits"][0]["action"] == "take_profit"
    ctx.conn.execute("UPDATE positions SET opened_at='2026-01-01T00:00:00+00:00'")  # held for ages
    ex = exits_once(ctx)["exits"][0]
    assert ex["action"] == "exit" and "max hold" in ex["reason"], ex
    assert rows(ctx.conn, "SELECT status FROM positions")[0]["status"] == "closed"


def test_a_fresh_position_down_a_little_is_held(tmp_path):
    ctx, hot, sent, at = _entered(tmp_path)
    hot.price_usd = at(0.8)  # a fresh position that looks down: no panic sell, the stop is -70%
    assert exits_once(ctx)["exits"][0]["action"] == "hold"


def test_marks_fall_back_to_dexscreener_when_jupiter_prices_are_down(tmp_path):
    ch, jup = _world()
    hot = ch.coins[mint(1)]
    ctx = _ctx(tmp_path, ch, jup, [_call(1, hot)])
    ctx.jupiter._call = _flaky(ctx.jupiter._call)  # no retry sleeps in the test
    trader_once(ctx)
    hot.price_usd *= 0.2
    jup.prices_down = True
    ex = exits_once(ctx)["exits"][0]
    assert ex["action"] == "exit" and "stop loss" in ex["reason"]  # marked from DexScreener, sold on a quote


def _flaky(call):
    def go(method, path, **kw):
        if path == "/price/v3":
            r = call.__self__._http.request(method, call.__self__.api_url + path, **kw)
            if r.status_code >= 500:
                from ct_agent.execution.jupiter import JupiterError
                raise JupiterError(f"{path}: HTTP {r.status_code}")
            return r.json()
        return call(method, path, **kw)
    return go


def test_call_outcomes_are_tracked_and_reported(tmp_path):
    from ct_agent.strategy.outcomes import report, track_outcomes
    ch, jup = _world()
    hot = ch.coins[mint(1)]
    ctx = _ctx(tmp_path, ch, jup, [_call(1, hot)])
    trader_once(ctx)
    r = rows(ctx.conn, "SELECT * FROM call_outcomes WHERE token=?", (hot.mint,))[0]
    assert r["action"] == "watch" and r["price0"] == pytest.approx(hot.price_usd / 150) and r["venue0"] == "pool"
    ctx.conn.execute("UPDATE call_outcomes SET decided_at='2026-01-01T00:00:00+00:00'")  # a day ago and more
    hot.price_usd *= 2
    assert track_outcomes(ctx) == 1
    r = rows(ctx.conn, "SELECT * FROM call_outcomes WHERE token=?", (hot.mint,))[0]
    assert r["p15m"] > r["price0"] and r["p24h"] is not None and r["done"] == 1
    rep = report(ctx.conn)
    assert rep["calls_tracked"] == 1 and rep["overall"]["hit_rate_+25%"] == 100
    assert "dex" in rep["by_variable"] and "pumpswap" in rep["by_variable"]["dex"]


def test_ten_trades_per_day_cap(tmp_path):
    from ct_agent.db import utcnow
    ch, jup = _world()
    ctx = _ctx(tmp_path, ch, jup, [], max_open_positions=50, max_total_exposure_sol=10)
    for i in range(10):
        ctx.conn.execute("INSERT INTO positions(token,mode,status,quote_symbol,opened_at,entry_quote,entry_tokens,"
                         "entry_price,closed_at,exit_quote,pnl_quote) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         (mint(100 + i), "paper", "closed", "SOL", utcnow(), 0.01, 1, 0.01, utcnow(), 0.01, 0))
    v = ctx.risk.check_entry(token=mint(1), quote_symbol="SOL", mode="paper")
    assert not v.ok and any("10 new trades" in r for r in v.reasons)


def test_repeat_calls_are_not_reposted(tmp_path):
    from ct_agent.social import compose
    from ct_agent.social.outbox import enqueue
    from ct_agent.strategy.engine import _thread
    ch, jup = _world()
    ctx = _ctx(tmp_path, ch, jup, [])
    tok = mint(1)
    assert _thread(ctx.conn, tok, "watch") == (False, None)
    enqueue(ctx.conn, compose.watch_note(symbol="HOT", token=tok, line="t", decision_id=1))
    assert _thread(ctx.conn, tok, "watch") == (True, "watch:1")        # same call again: skipped
    assert _thread(ctx.conn, tok, "pass") == (False, "watch:1")        # new call: threads under it
    assert _thread(ctx.conn, tok, "trade_entry") == (False, "watch:1")


def _chat_api(tmp_path, coin=None, **kw):
    ch, jup = _world(coin)
    grok = _grok()
    ctx = _ctx(tmp_path, ch, jup, [], grok=grok, **kw)
    app = create_app(ctx.settings)
    app.state.chat_services = {"rpc": ctx.rpc, "market": ctx.market, "jupiter": ctx.jupiter, "brain": ctx.brain,
                               "web": ctx.web}
    return TestClient(app), ch.coins[mint(1)], grok, ctx


def test_ask_vigil_researches_a_mint(tmp_path):
    from ct_agent.api import chat
    chat._RESEARCH.clear()
    api, hot, grok, ctx = _chat_api(tmp_path)
    d = api.post("/chat", json={"message": "Research $HOT", "token": hot.mint}).json()  # a suggestion chip
    card = d["token"]
    assert card["symbol"] == "HOT" and card["explorer"] == f"https://solscan.io/token/{hot.mint}", d
    assert (card["stage"], card["dex"]) == ("pool", "pumpswap")
    assert isinstance(card["liquidity_usd"], (int, float)) and isinstance(card["mcap_usd"], (int, float))
    assert card["mcap_usd"] == round(hot.price_usd * 1e9) and card["liquidity_usd"] == 6_000_000
    assert set(card) == {"address", "name", "symbol", "stage", "dex", "liquidity_usd", "mcap_usd", "explorer", "logo"}
    chat._RESEARCH.clear()
    r = api.post("/chat", json={"message": f"what do you think of {hot.mint}?"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["reply"].startswith("HOT is a cat coin") and d["remaining"]["hour"] == 8
    sent = grok.calls[-1]
    assert '"mint_safety"' in sent and '"holders"' in sent and "<visitor_conversation>" in sent

    d = api.post("/chat", json={"message": "is $hot risky?"}).json()  # ticker lookup (token cache), any case
    assert d["token"]["address"] == hot.mint

    # a follow-up with no coin named: the earlier coin goes to Grok quietly (no card), marked as context
    d = api.post("/chat", json={"message": "and the holders?", "context_token": hot.mint}).json()
    assert d["token"] is None and '"context_only": true' in grok.calls[-1] and '"symbol": "HOT"' in grok.calls[-1]

    d = api.post("/chat", json={"message": f"check {mint(404)}"}).json()  # a valid address, but no token mint
    assert d["token"] is None and '"is_solana_token": false' in grok.calls[-1]
    assert api.post("/chat", json={"message": "hi", "token": "0x" + "ab" * 20}).status_code == 422  # EVM refused

    assert api.post("/chat", json={"message": "x" * 501}).status_code == 400
    pre = api.options("/chat", headers={"origin": "https://site.example", "access-control-request-method": "POST",
                                        "access-control-request-headers": "authorization"})
    assert pre.status_code == 400  # no Authorization header from browsers: admin calls stay server-side


def test_ticker_falls_back_to_dexscreener_search(tmp_path):
    from ct_agent.api.chat import find_token
    api, hot, grok, ctx = _chat_api(tmp_path)
    assert find_token(ctx.conn, "what about $HOT", ctx.market) == (hot.mint, "ticker")  # not seen before: searched
    assert find_token(ctx.conn, "what about $NOPE", ctx.market)[0] is None


def test_ask_vigil_is_rate_limited(tmp_path):
    api, hot, grok, ctx = _chat_api(tmp_path, chat_per_ip_hour=2, chat_global_day=3)
    one = {"x-forwarded-for": "1.1.1.1"}
    assert api.get("/chat/limits", headers=one).json()["remaining"] == {"hour": 2, "day": 30}
    for _ in range(2):
        assert api.post("/chat", json={"message": "hi"}, headers=one).status_code == 200
    r = api.post("/chat", json={"message": "hi"}, headers=one)
    assert r.status_code == 429 and "questions this hour" in r.json()["detail"]
    assert api.post("/chat", json={"message": "hi"}, headers={"x-forwarded-for": "9.9.9.9, 1.1.1.1"}).status_code == 429
    assert api.post("/chat", json={"message": "hi"}, headers={"x-forwarded-for": "2.2.2.2"}).status_code == 200
    r = api.post("/chat", json={"message": "hi"}, headers={"x-forwarded-for": "3.3.3.3"})
    assert r.status_code == 429 and "all it can for today" in r.json()["detail"]
    assert len(grok.calls) == 3  # refused requests never reached Grok


def test_chat_streams_each_research_step(tmp_path):
    from ct_agent.api import chat
    chat._RESEARCH.clear()
    hot = _hot(description="HOT is the cat who guards every fridge on Solana.", twitter="https://x.com/hotcoin")
    api, hot, grok, ctx = _chat_api(tmp_path, hot)
    r = api.post("/chat/stream", json={"message": f"look at {hot.mint}"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/x-ndjson")
    ev = [json.loads(line) for line in r.text.splitlines() if line.strip()]
    done = [(e["id"], e["status"]) for e in ev if e["type"] == "step" and e["status"] != "run"]
    assert done == [("coin", "done"), ("onchain", "done"), ("lore", "done"), ("x", "skip"), ("website", "skip"),
                    ("grok", "done")], done
    steps = {e["id"]: e["detail"] for e in ev if e["type"] == "step" and e["status"] != "run"}
    assert steps["coin"] == "$HOT · Hot" and "pool on pumpswap" in steps["onchain"]
    assert "guards every fridge" in steps["lore"] and steps["x"] == "@hotcoin (profile not checked)"
    assert ev[-1]["type"] == "answer" and "guards every fridge" in grok.calls[-1].split("<untrusted_evidence>")[1]
    ev = [json.loads(line) for line in api.post("/chat/stream", json={"message": "how does VIGIL pick trades?"}).text.splitlines()]
    assert [e["id"] for e in ev if e["type"] == "step"] == ["grok", "grok"] and ev[-1]["type"] == "answer"


def test_launchpad_status_reads_the_new_columns(tmp_path):
    from ct_agent.db import utcnow
    ch, jup = _world()
    ctx = _ctx(tmp_path, ch, jup, [])
    rcpt = wallet(5)
    ctx.conn.execute("INSERT INTO launch_requests(x_post_id,author_id,handle,created_at,status,name,symbol,recipient,"
                     "token,tx_hash,fee_shares,launched_at) VALUES('77','a','anon',?,'launched','Cat','CAT',?,?,?,?,?)",
                     (utcnow(), rcpt, mint(9), "5sig", json.dumps([{"address": rcpt, "bps": 9000}]), utcnow()))
    api = TestClient(create_app(ctx.settings))
    d = api.get("/launchpad/status", params={"symbol": "$cat", "wallet": rcpt}).json()
    assert d["status"] == "launched" and d["token"] == mint(9) and d["tx_hash"] == "5sig"
    assert d["fee_shares"] == [{"address": rcpt, "bps": 9000}] and d["launched_at"]
    assert "pair_symbol" not in d
    assert api.get("/launchpad/status", params={"symbol": "CAT", "wallet": rcpt.lower()}).json() == {"status": "not_seen"}
    recent = api.get("/launchpad/recent").json()
    assert recent[0]["token"] == mint(9) and recent[0]["fee_shares"][0]["bps"] == 9000
    assert "fees_handed_over" not in recent[0] and "pair_symbol" not in recent[0]


def test_v1_history_folds_into_the_scorecard():
    from ct_agent.api.site import _with_v1
    now = {"closed": 2, "taken": 2, "wins": 0, "losses": 2, "win_rate": 0.0, "net_sol": -0.05, "avg_sol": -0.025,
           "best": {"symbol": "MCAT", "pnl_sol": -0.005, "pnl_pct": -10.0}, "worst": {"symbol": "FOLD", "pnl_sol": -0.045},
           "deployed_sol": 0.1, "return_pct": -50.0, "avg_hold_h": 1.0, "series": [0.0, -0.005, -0.05], "labels": ["MCAT", "FOLD"]}
    assert _with_v1(now, {}) is now
    sc = _with_v1(now, {"trades": 100, "wins": 54, "losses": 46, "best_sol": 0.3})
    assert (sc["closed"], sc["wins"], sc["losses"], sc["win_rate"]) == (102, 54, 48, 52.9)
    assert sc["best"]["pnl_sol"] == 0.3 and sc["best"]["v1"] is True
    assert sc["net_sol"] == -0.05 and sc["worst"]["symbol"] == "FOLD"  # v1 didn't give these: current bot's own
    assert sc["history"] == {"v1_trades": 100, "v1_wins": 54, "v1_losses": 46, "includes": ["best_sol"]}
    sc = _with_v1(now, {"trades": 100, "wins": 54, "net_sol": 0.35, "deployed_sol": 5.0, "avg_hold_h": 3.0})
    assert sc["net_sol"] == 0.3 and sc["deployed_sol"] == 5.1 and sc["return_pct"] == 5.88
    assert "net_sol" in sc["history"]["includes"]
    assert sc["avg_hold_h"] == 2.96 and sc["series"] == [0.0, 0.35, 0.345, 0.3] and sc["labels"][0] == "VIGIL v1 (100 trades)"


def test_admin_can_queue_a_hand_written_post(tmp_path):
    api = TestClient(create_app(_settings(tmp_path)))
    assert api.post("/admin/posts", json={"text": "gm"}).status_code == 401
    h = {"authorization": "Bearer admin-secret"}
    r = api.post("/admin/posts", json={"text": "Thinking about deploying my own coin today."}, headers=h)
    assert r.status_code == 200 and r.json()["queued"] is True
    assert api.post("/admin/posts", json={"text": "Thinking about deploying my own coin today."}, headers=h).json()["queued"] is False
    assert api.post("/admin/posts", json={"text": "x" * 281}, headers=h).status_code == 422
    pending = api.get("/posts", params={"status": "pending"}).json()
    assert len(pending) == 1 and pending[0]["kind"] == "note"
