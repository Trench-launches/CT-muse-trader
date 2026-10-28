"""The smart-money scanner feed: Solana only, the +30% trigger, the tradeable check, and the site's call list."""
import json
from datetime import datetime, timedelta, timezone

from ct_agent.chain.rpc import RpcError
from ct_agent.context import build
from ct_agent.sources import scanner

from .fakes import Coin, FakeJupiter, FakeSolana, market_transport, mint, wallet


def _call(i, token, price, hours_ago):
    ts = int((datetime.now(timezone.utc) - timedelta(hours=hours_ago)).timestamp() * 1000)
    return {"id": i, "token_address": token, "token_symbol": f"C{i}", "alert_price": price, "alert_mcap": 100_000,
            "alert_timestamp": ts, "alert_signals": json.dumps({"positive": ["SM net buy $5k"]}), "source": "nansen_screener"}


def _ctx(settings, coins, calls, fibbot=None, **kw):
    ch = FakeSolana(coins)
    seen: list[str] = []
    s = settings.model_copy(update={"supabase_url": "https://x.supabase.co", "supabase_key": "k",
                                    "jupiter_api_key": "test", **kw})
    ctx = build(s, rpc_transport=ch.transport(), jupiter_transport=FakeJupiter(ch).transport(),
                market_transport=market_transport(ch, supabase=calls, fibbot=fibbot, seen=seen))
    return ctx, ch, seen


def test_scanner_only_hands_over_calls_that_ran_30pct(settings):
    a, b, c, d, e = (Coin(mint(n), price_usd=p) for n, p in ((1, 1.35), (2, 1.10), (3, 2.0), (4, 2.0), (5, 1.50)))
    c.mint_authority = wallet(1)  # the creator can still mint: never tradeable
    d.routable = False            # Jupiter has no route
    calls = [_call(1, a.mint, 1.0, 1), _call(2, b.mint, 1.0, 1), _call(3, c.mint, 1.0, 1), _call(4, d.mint, 1.0, 1),
             _call(5, e.mint, 1.0, 5)]
    ctx, ch, seen = _ctx(settings, [a, b, c, d, e], calls)
    assert scanner.fetch_new(ctx) == 5
    assert all("chain=eq.solana" in u for u in seen if "alert_performance" in u)
    ready = scanner.refresh(ctx)
    assert [r["token"] for r in ready] == [a.mint, e.mint]  # +35% and +50%; b is only +10%
    st = {r["token"]: dict(r) for r in ctx.conn.execute("SELECT token, status, note, last_move_pct, pool_id FROM scanner_calls")}
    assert st[c.mint]["status"] == "skipped" and st[c.mint]["note"] == "unsafe coin: its creator can still mint more coins"
    assert st[d.mint]["status"] == "skipped" and "Jupiter can't route" in st[d.mint]["note"]
    assert st[b.mint]["status"] == "watching" and round(st[b.mint]["last_move_pct"]) == 10
    assert st[a.mint]["pool_id"] == a.pool  # the deepest SOL pool
    assert ctx.conn.execute("SELECT decimals FROM tokens WHERE mint=?", (a.mint,)).fetchone()["decimals"] == 6

    # past the window, a call is dropped even if it's up
    ctx.conn.execute("UPDATE scanner_calls SET called_at=? WHERE token=?",
                     ((datetime.now(timezone.utc) - timedelta(hours=7)).isoformat(timespec="seconds"), e.mint))
    assert [r["token"] for r in scanner.refresh(ctx)] == [a.mint]
    scanner.mark_bought(ctx.conn, a.mint.lower())  # a lowercased mint is another address: nothing marked
    assert [r["token"] for r in scanner.refresh(ctx)] == [a.mint]
    scanner.mark_bought(ctx.conn, a.mint)
    assert scanner.refresh(ctx) == []
    facts = scanner.call_facts(dict(ctx.conn.execute("SELECT * FROM scanner_calls WHERE token=?", (a.mint,)).fetchone()), 30)
    assert facts["signals"] == {"positive": ["SM net buy $5k"]} and facts["move_since_call_pct"] == 35.0


def test_scanner_never_trades_a_skipped_token(settings):
    old = Coin(mint(6), price_usd=2.0)  # e.g. the project's own older coin, listed in CT_SKIP_TOKENS
    ctx, ch, _ = _ctx(settings, [old], [_call(1, old.mint, 1.0, 1)], skip_tokens=[old.mint])
    scanner.fetch_new(ctx)
    assert scanner.refresh(ctx) == []  # up 100%, tradeable, and still skipped
    note = ctx.conn.execute("SELECT status, note FROM scanner_calls WHERE token=?", (old.mint,)).fetchone()
    assert note["status"] == "skipped" and "tied to" in note["note"]


def test_a_check_that_cant_run_is_retried_not_skipped(settings):
    a = Coin(mint(1), price_usd=1.5)
    ctx, ch, _ = _ctx(settings, [a], [_call(1, a.mint, 1.0, 1)])
    scanner.fetch_new(ctx)

    def rate_limited(*a, **k):
        raise RpcError("429 too many requests")
    ctx.rpc.account_info = rate_limited
    assert scanner.refresh(ctx) == []
    row = ctx.conn.execute("SELECT status, pool_id FROM scanner_calls").fetchone()
    assert row["status"] == "watching" and row["pool_id"] is None  # tried again next cycle
    del ctx.rpc.account_info
    assert [r["token"] for r in scanner.refresh(ctx)] == [a.mint]


def test_fibbot_recalls_are_solana_and_deduped(settings):
    a = Coin(mint(1), price_usd=1.0)
    at = datetime.now(timezone.utc) - timedelta(hours=1)
    fib = [{"id": "0a1b2c3d-0000-0000-0000-000000000000", "token_address": a.mint, "symbol": "A",
            "called_at": (at + timedelta(minutes=5)).isoformat(), "call_price": 1.0, "call_mc": 1, "source": "smart_money"},
           {"id": "0b1b2c3d-0000-0000-0000-000000000000", "token_address": a.mint, "symbol": "A",
            "called_at": (at + timedelta(hours=2)).isoformat(), "call_price": 1.0, "call_mc": 1, "source": "smart_money"}]
    ctx, ch, seen = _ctx(settings, [a], [_call(1, a.mint, 1.0, 1)], fibbot=fib,
                         fibbot_supabase_url="https://fib.supabase.co", fibbot_supabase_key="k")
    scanner.fetch_new(ctx)
    assert scanner.fetch_fibbot(ctx) == 1  # the first is the same call as the main scanner's; the second is a re-call
    assert any("smart_money_calls" in u and "chain=eq.solana" in u for u in seen)


def test_site_groups_trade_tweets_into_one_thread_per_coin(settings, db):
    from ct_agent.api.site import _CACHE, site_data
    _CACHE.clear()
    s = settings.model_copy(update={"score_since": "2026-09-25T00:00:00+00:00"})
    A, B, C, D = mint(1), mint(2), mint(3), mint(4)
    posts = [("entry:live:1", "trade_entry", "Bought $A", A, "2026-09-25T01:00:00+00:00", "11"),
             ("analysis:1", "analysis", "Why I bought $A:", A, "2026-09-25T01:00:05+00:00", "12"),
             ("musing:1", "musing", "gm", "", "2026-09-25T01:30:00+00:00", "13"),
             ("entry:live:2", "trade_entry", "Bought $B", B, "2026-09-25T02:00:00+00:00", "14"),
             ("update:live:1:+50", "update", "$A update: +50%", A, "2026-09-25T03:00:00+00:00", "15"),
             ("old", "trade_entry", "Bought $OLD", C, "2026-09-24T01:00:00+00:00", "10"),
             ("pending", "trade_entry", "Bought $D", D, "2026-09-25T04:00:00+00:00", None)]
    for key, kind, body, ref, at, xid in posts:
        db.execute("INSERT INTO posts(idempotency_key,kind,body,ref,status,x_post_id,created_at,sent_at) "
                   "VALUES(?,?,?,?,?,?,?,?)", (key, kind, body, ref, "sent" if xid else "pending", xid, at, at))
    th = site_data(db, s)["trade_threads"]
    assert [t["token"] for t in th] == [B, A]  # newest buy first; musings, old and unsent posts left out
    assert [p["x_post_id"] for p in th[1]["posts"]] == ["11", "12", "15"]  # the buy, its reasons, the update


def test_site_shows_the_held_calls_trade_pnl(settings, db):
    from ct_agent.api.site import _CACHE, site_data
    from ct_agent.db import kv_set
    _CACHE.clear()
    tok = mint(1)
    db.execute("INSERT INTO positions(token,mode,status,decision_id,quote_symbol,opened_at,entry_quote,entry_tokens,"
               "entry_price) VALUES(?,?,?,?,?,?,?,?,?)", (tok, "paper", "open", None, "SOL", "2026-09-25T08:13:42+00:00",
                                                          0.5, 1000.0, 5e-4))
    db.execute("INSERT INTO scanner_calls(id,token,symbol,called_at,call_price_usd,data,status,first_seen,last_move_pct) "
               "VALUES(1,?,'CALL','2026-09-25T01:00:00+00:00',1.0,'{}','bought','2026-09-25T01:00:00+00:00',38.6)", (tok,))
    db.execute("INSERT INTO tokens(mint,symbol,decimals,stage,dex_id,first_seen) VALUES(?,?,?,?,?,?)",
               (tok, "CALL", 6, "curve", "pumpfun", "2026-09-25T01:00:00+00:00"))
    kv_set(db, "marks:1", [0.6])
    d = site_data(db, settings)
    w = d["watching"][0]
    assert (w["token"], w["status"], w["move_pct"], w["stage"], w["dex"]) == (tok, "bought", 20.0, "curve", "pumpfun")
    p = d["positions"][0]
    assert p["entry_sol"] == 0.5 and p["pnl_pct"] == 20.0 and p["symbol"] == "CALL" and p["stage"] == "curve"
    assert d["score"]["unrealized_sol"] == 0.1 and "watch_mode" not in d and "pons_scan" not in d
