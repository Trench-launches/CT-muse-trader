"""Risk limits, the paper broker, fill pricing, the live Jupiter executor and the screen."""
from pathlib import Path
from types import SimpleNamespace

import base58
import pytest
from solders.keypair import Keypair

from ct_agent.chain.solana import SolanaRpc
from ct_agent.config import Settings
from ct_agent.db import journal_recent, rows
from ct_agent.execution.jupiter import Jupiter
from ct_agent.execution.live import LiveExecutor, LiveTradingBlocked
from ct_agent.execution.paper import close_position, open_position
from ct_agent.execution.pricing import FillEstimate, from_quote, impact_bps, pool_estimate
from ct_agent.execution.risk import RiskManager
from ct_agent.strategy.screen import screen_token

from .fakes import Coin, FakeJupiter, FakeSolana, mint

KEY = Keypair()
SECRET = base58.b58encode(bytes(KEY)).decode()


def _fill(sol=0.05, tokens=1_000_000.0) -> FillEstimate:
    return FillEstimate(sol, tokens, sol / tokens, sol / tokens, 10, 0.0, "jupiter_quote")


def test_pool_estimate_is_labelled_indicative_and_impact_excludes_fees():
    e = pool_estimate(0.05, 1e-6, 40_000, 150, 100)
    assert e.method == "pool_estimate_indicative" and e.tokens_out > 0
    assert pool_estimate(0.0001, 1e-6, 100_000, 150, 700).price_impact_bps <= 1  # dust: fees aren't impact
    sell = pool_estimate(1000.0, 1e-6, 100_000, 150, 100, buy=False)
    assert sell.quote_in < 1000 * 1e-6


def test_jupiter_order_becomes_a_fill():
    buy = from_quote({"inAmount": "500000000", "outAmount": "750000000000", "priceImpact": -0.25}, buy=True, decimals=6)
    assert (buy.quote_in, buy.tokens_out, buy.price_impact_bps) == (0.5, 750_000.0, 25)
    sell = from_quote({"inAmount": "750000000000", "outAmount": "490000000", "priceImpactPct": "0.004"}, buy=False,
                      decimals=6)
    assert (sell.quote_in, sell.tokens_out, sell.price_impact_bps) == (0.49, 750_000.0, 40)
    assert impact_bps({}) == 0


def test_risk_limits_and_kill_switch(db, settings):
    r = RiskManager(settings, db)
    assert r.check_entry(token=mint(1), quote_symbol="SOL", mode="paper").ok
    assert not r.check_entry(token=mint(1), quote_symbol="USDC", mode="paper").ok
    live = r.check_entry(token=mint(1), quote_symbol="SOL", mode="live")
    assert not live.ok and "trading mode is paper" in live.reasons
    Path(settings.kill_switch_file).write_text("stop")
    v = r.check_entry(token=mint(1), quote_symbol="SOL", mode="paper")
    assert not v.ok and "kill file" in v.reasons[0]
    Path(settings.kill_switch_file).unlink()
    r.set_kill(True, "manual test", "tester")
    assert r.kill_active()[0]
    r.set_kill(False, "resume", "tester")
    assert not r.kill_active()[0]


def test_paper_lifecycle_limits_and_journal(db, settings):
    r = RiskManager(settings, db)
    for i in range(settings.max_open_positions):
        v = r.check_entry(token=mint(i), quote_symbol="SOL", mode="paper")
        assert v.ok, v.reasons
        assert v.size_sol == settings.max_position_sol == 0.5
        open_position(db, token=mint(i), symbol="T", decision_id=None, quote_symbol="SOL", fill=_fill(), stage="pool")
    v = r.check_entry(token=mint(9), quote_symbol="SOL", mode="paper")
    assert not v.ok and any("max open" in x for x in v.reasons)
    assert not r.check_entry(token=mint(0), quote_symbol="SOL", mode="paper").ok  # duplicate
    # the same mint lowercased is a different (invalid) address: never matched as a duplicate
    assert "already holding this token" not in r.check_entry(token=mint(0).lower(), quote_symbol="SOL", mode="paper").reasons
    pos = rows(db, "SELECT * FROM positions WHERE token=?", (mint(0),))[0]
    pnl = close_position(db, pos, fill=_fill(0.02), reason="stop_loss")
    assert pnl == pytest.approx(-0.03)
    kinds = [j["kind"] for j in journal_recent(db)]
    assert kinds.count("paper_fill") == 3 and "paper_exit" in kinds
    assert all(j["payload"]["onchain"] is False for j in journal_recent(db, kind="paper_fill"))


def test_daily_loss_limit_blocks_new_entries(db, settings):
    db.execute("INSERT INTO positions(token,mode,status,quote_symbol,opened_at,entry_quote,entry_tokens,entry_price,"
               "closed_at,pnl_quote) VALUES(?,'paper','closed','SOL',datetime('now'),0.5,1,0.5,"
               "strftime('%Y-%m-%dT%H:%M:%S+00:00','now'),-0.6)", (mint(1),))
    v = RiskManager(settings, db).check_entry(token=mint(2), quote_symbol="SOL", mode="paper")
    assert not v.ok and any("daily loss limit 0.5 SOL" in x for x in v.reasons)


def test_trade_size_is_a_share_of_the_account(db, settings):
    from ct_agent.strategy.engine import _trade_size
    s = settings.model_copy(update={"position_size_pct": 3.0, "paper_starting_balance_sol": 1.0,
                                    "max_position_sol": 0.5})
    ctx = SimpleNamespace(settings=s, risk=RiskManager(s, db))
    assert _trade_size(ctx) == 0.03
    db.execute("INSERT INTO positions(token,mode,status,quote_symbol,opened_at,entry_quote,entry_tokens,entry_price,"
               "closed_at,pnl_quote) VALUES(?,'paper','closed','SOL','2026-01-01',0.05,1,0.05,'2026-01-01',1.0)",
               (mint(1),))
    assert _trade_size(ctx) == 0.06  # the account doubled, so does the size
    assert RiskManager(s, db).check_entry(token=mint(2), quote_symbol="SOL", mode="paper",
                                          desired_sol=_trade_size(ctx)).size_sol == 0.06
    assert _trade_size(SimpleNamespace(settings=settings, risk=RiskManager(settings, db))) is None  # 0% = fixed size


# --- the live executor ------------------------------------------------------------------------------------------

def _live(db, tmp_path, coin=None, **kw):
    ch = FakeSolana([coin or Coin(mint(1), price_usd=0.0001)])
    jup = FakeJupiter(ch)
    s = Settings(_env_file=None, trading_mode="live", live_execution_reviewed=True, trading_wallet_private_key=SECRET,
                 kill_switch_file=str(tmp_path / "KILL"), jupiter_api_key="test", **kw)
    rpc = SolanaRpc("https://rpc.test", transport=ch.transport(), retries=0)
    j = Jupiter("https://api.jup.ag", "test", transport=jup.transport(), min_interval_s=0)
    ex = LiveExecutor(s, rpc, j, db, RiskManager(s, db))
    ch.lamports[ex.wallet] = 2 * 10**9
    return ex, ch, jup


def test_live_executor_refuses_by_default(db, settings):
    ex = LiveExecutor(settings, SimpleNamespace(), SimpleNamespace(), db, RiskManager(settings, db))
    assert set(ex.blockers()) >= {"trading mode is paper", "live execution adapter not marked reviewed",
                                  "no trading wallet key"}
    with pytest.raises(LiveTradingBlocked, match="trading mode is paper"):
        ex.swap(mint(1), "buy", 10**7)
    bad = settings.model_copy(update={"trading_mode": "live", "live_execution_reviewed": True,
                                      "trading_wallet_private_key": "0x" + "11" * 32})
    ex = LiveExecutor(bad, SimpleNamespace(), SimpleNamespace(), db, RiskManager(bad, db))
    assert ex.wallet is None and any("not a valid Solana key" in b for b in ex.blockers())


def test_live_buy_then_sell_through_jupiter(db, tmp_path):
    ex, ch, jup = _live(db, tmp_path)
    assert ex.blockers() == [] and ex.wallet == str(KEY.pubkey())
    buy = ex.swap(mint(1), "buy", 10**7)  # 0.01 SOL
    assert buy.quote_amount == 10**7 and buy.token_amount == int(0.01 / (0.0001 / 150) * 0.999 * 10**6)
    assert ch.balances[(ex.wallet, mint(1))] == buy.token_amount and ch.statuses[buy.tx_hash]
    sub = journal_recent(db, kind="live_submitted")[0]["payload"]
    assert sub["tx_hash"] == buy.tx_hash and sub["price_impact_bps"] == 10
    sell = ex.swap(mint(1), "sell", buy.token_amount)
    assert sell.token_amount == buy.token_amount and 0 < sell.quote_amount < 10**7  # the round trip costs a little
    fills = journal_recent(db, kind="live_fill")
    assert [f["payload"]["side"] for f in fills] == ["sell", "buy"] and fills[0]["payload"]["onchain"] is True
    assert ex.quote_sell(mint(1), 10**6) > 0


def test_live_impact_gate_refuses_before_signing(db, tmp_path):
    ex, ch, jup = _live(db, tmp_path, Coin(mint(1), impact_pct=4.0))  # 400 bps > 300
    with pytest.raises(LiveTradingBlocked, match="price impact 400 bps > 300"):
        ex.swap(mint(1), "buy", 10**7)
    assert "/swap/v2/execute" not in jup.calls and not ch.balances
    assert journal_recent(db, kind="live_failed")[0]["payload"]["stage"] == "price_impact"


def test_live_refuses_other_chains_and_low_balance(db, tmp_path):
    ex, ch, jup = _live(db, tmp_path)
    ch.lamports[ex.wallet] = 5 * 10**6
    with pytest.raises(LiveTradingBlocked, match="balance too low"):
        ex.swap(mint(1), "buy", 10**7)
    with pytest.raises(LiveTradingBlocked, match="not a Solana token"):
        ex.swap("0x" + "ab" * 20, "buy", 10)
    ex2, ch2, _ = _live(db, tmp_path)
    ch2.genesis = "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG"  # devnet
    with pytest.raises(LiveTradingBlocked, match="not Solana mainnet"):
        ex2.swap(mint(1), "buy", 10**7)


def test_live_failed_swap_is_not_a_fill(db, tmp_path):
    ex, ch, jup = _live(db, tmp_path)
    jup.execute_error = "slippage tolerance exceeded"
    with pytest.raises(LiveTradingBlocked, match="not confirmed"):
        ex.swap(mint(1), "buy", 10**7)
    assert journal_recent(db, kind="live_failed")[0]["payload"]["stage"] == "execute"
    assert not journal_recent(db, kind="live_fill")


def test_selling_everything_survives_float_rounding(db, tmp_path):
    ex, ch, jup = _live(db, tmp_path)
    held = 622314834701
    ch.balances[(ex.wallet, mint(1))] = held
    want = int(622314.8347010531 * 10**6) + 1
    assert want > held and ex._sellable(mint(1), want) == held  # rounding: sell exactly what's there
    assert ex._sellable(mint(1), held // 2) == held // 2        # a partial sell is untouched
    with pytest.raises(LiveTradingBlocked, match="token balance"):
        ex._sellable(mint(1), held * 2)                          # really missing tokens: refused


def test_live_mark_and_quote_go_to_sol(db, tmp_path):
    ex, ch, jup = _live(db, tmp_path)
    jup.calls.clear()
    ex.quote_sell(mint(1), 10**6)
    order = [o for o in jup.orders.values()]
    assert jup.calls == ["/swap/v2/order"] and order == []  # a quote has no taker: nothing to sign


# --- screen ------------------------------------------------------------------------------------------------------

def _scr(settings, **kw):
    args = dict(token=mint(1), stage="pool", pool_found=True, volume_h1_usd=30_000, liquidity_usd=45_000,
                volume_h24_usd=240_000)
    args.update(kw)
    return screen_token(settings, **args)


def test_screen_rules(settings):
    good = _scr(settings, holders={"top10_share": 0.2, "top_holder_share": 0.05, "creator_share": 0.0})
    assert good.eligible and good.metrics["volume_heat"] == 3.0 and good.stage == "pool"
    for kw, msg in [({"pool_found": False}, "no market data"), ({"volume_h1_usd": 4_000}, "1h volume"),
                    ({"liquidity_usd": 10_000}, "liquidity"), ({"quote_symbol": "USDC"}, "priced in USDC"),
                    ({"safety": ["its creator can still mint more coins"]}, "unsafe mint"),
                    ({"holders": {"top10_share": 0.5}}, "10 biggest wallets hold 50%"),
                    ({"holders": {"creator_share": 0.2}}, "creator still holds 20%")]:
        assert any(msg in b for b in _scr(settings, **kw).blockers), kw
    wash = _scr(settings, volume_h1_usd=300_000)
    assert any("wash" in f for f in wash.flags)
