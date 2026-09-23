from pathlib import Path

import pytest

from ct_agent.chain.pons import PonsV2
from ct_agent.chain.rpc import RpcClient
from ct_agent.config import Settings
from ct_agent.db import journal_recent, rows
from ct_agent.execution.live import LiveExecutor, LiveTradingBlocked
from ct_agent.execution.paper import close_position, open_position
from ct_agent.execution.pricing import curve_buy, curve_sell, pool_estimate
from ct_agent.execution.risk import RiskManager
from ct_agent.strategy.screen import Activity, WindowStats, activity_for, screen_almost_bonded, screen_migrated

from .fakes import FACTORY, FakeChain, FakeToken, addr

ETH = 10**18


def test_curve_math_matches_constant_product_with_fees():
    est = curve_buy(ETH // 10, int(1.68 * ETH), 10**27, 100, 0)
    net = ETH // 10 - ETH // 1000
    assert est.tokens_out == pytest.approx((10**27 * net // (int(1.68 * ETH) + net)) / 1e18)
    assert est.price_impact_bps > 500  # 0.1 ETH into a 1.68 ETH curve moves price a lot
    back = curve_sell(int(est.tokens_out * 1e18), int(1.68 * ETH) + net, 10**27 - int(est.tokens_out * 1e18), 100, 0)
    assert back.quote_in < 0.1  # round trip loses fees


def test_pool_estimate_is_labelled_indicative():
    e = pool_estimate(0.05, 1e-8, 40_000, 2_700, 100)
    assert e.method == "pool_estimate_indicative" and e.tokens_out > 0


def test_risk_limits_and_kill_switch(db, settings):
    r = RiskManager(settings, db)
    assert r.check_entry(token="0x1", quote_symbol="ETH", mode="paper").ok
    assert not r.check_entry(token="0x1", quote_symbol="USDC", mode="paper").ok
    live = r.check_entry(token="0x1", quote_symbol="ETH", mode="live")
    assert not live.ok and "trading mode is paper" in live.reasons
    Path(settings.kill_switch_file).write_text("stop")
    v = r.check_entry(token="0x1", quote_symbol="ETH", mode="paper")
    assert not v.ok and "kill file" in v.reasons[0]
    Path(settings.kill_switch_file).unlink()
    r.set_kill(True, "manual test", "tester")
    assert r.kill_active()[0]
    r.set_kill(False, "resume", "tester")
    assert not r.kill_active()[0]


def test_paper_lifecycle_limits_and_journal(db, settings):
    r = RiskManager(settings, db)
    fill = curve_buy(int(0.05 * ETH), int(5 * ETH), 3 * 10**26, 100, 0)
    for i in range(settings.max_open_positions):
        v = r.check_entry(token=f"0x{i}", quote_symbol="ETH", mode="paper")
        assert v.ok, v.reasons
        open_position(db, token=f"0x{i}", symbol="T", decision_id=None, quote_symbol="ETH", fill=fill,
                      stage="almost_bonded")
    v = r.check_entry(token="0x9", quote_symbol="ETH", mode="paper")
    assert not v.ok and any("max open" in x for x in v.reasons)
    assert not r.check_entry(token="0x0", quote_symbol="ETH", mode="paper").ok  # duplicate
    pos = rows(db, "SELECT * FROM positions WHERE token='0x0'")[0]
    exit_fill = curve_sell(int(fill.tokens_out * 1e18), int(4 * ETH), 4 * 10**26, 100, 0)
    pnl = close_position(db, pos, fill=exit_fill, reason="stop_loss")
    assert pnl < 0
    kinds = [j["kind"] for j in journal_recent(db)]
    assert kinds.count("paper_fill") == 3 and "paper_exit" in kinds
    assert all(j["payload"]["onchain"] is False for j in journal_recent(db, kind="paper_fill"))


def test_daily_loss_limit_blocks_new_entries(db, settings):
    db.execute("INSERT INTO positions(token,mode,status,quote_symbol,opened_at,entry_quote,entry_tokens,entry_price,"
               "closed_at,pnl_quote) VALUES('0xa','paper','closed','ETH',datetime('now'),0.05,1,0.05,"
               "strftime('%Y-%m-%dT%H:%M:%S+00:00','now'),-0.06)")
    v = RiskManager(settings, db).check_entry(token="0xb", quote_symbol="ETH", mode="paper")
    assert not v.ok and any("daily loss" in x for x in v.reasons)


def test_live_executor_refuses_by_default(db, settings):
    ch = FakeChain()
    pons = PonsV2(RpcClient("http://fake", transport=ch.transport(), retries=0), FACTORY)
    ex = LiveExecutor(settings, pons.rpc, pons, db, RiskManager(settings, db))
    assert set(ex.blockers()) >= {"trading mode is paper", "live execution adapter not marked reviewed",
                                  "no trading wallet key"}
    with pytest.raises(LiveTradingBlocked):
        ex.buy_on_curve(token=addr(1), curve=addr(2), quote_in_wei=ETH // 100, fee_bps=100, tax_bps=0)
    with pytest.raises(LiveTradingBlocked, match="paper-only"):
        ex.swap_on_pool()
    s2 = Settings(_env_file=None, trading_mode="live", live_execution_reviewed=True,
                  trading_wallet_private_key="0x" + "11" * 32, kill_switch_file=settings.kill_switch_file)
    ex2 = LiveExecutor(s2, pons.rpc, pons, db, RiskManager(s2, db))
    assert ex2.blockers() == [] and ex2.wallet.startswith("0x")


def _act(**kw):
    base = dict(windows=[WindowStats(300, 10, 1, 1.0, 0.1, 8)], top_buyer_share=0.1, deployer_buy_share=0.0,
                roundtrip_traders=0, accelerating=True, acceleration=3.0)
    base.update(kw)
    return Activity(**base)


def test_screen_almost_bonded_rules(settings):
    ok = screen_almost_bonded(settings, token="0x1", progress=0.9, phase=0, launch_age_s=600, snipe_window_s=3,
                              fee_bps=100, creator_tax_bps=0, activity=_act())
    assert ok.eligible and ok.score > 0
    low = screen_almost_bonded(settings, token="0x1", progress=0.5, phase=0, launch_age_s=600, snipe_window_s=3,
                               fee_bps=100, creator_tax_bps=0, activity=_act())
    assert not low.eligible
    snipe = screen_almost_bonded(settings, token="0x1", progress=0.95, phase=0, launch_age_s=2, snipe_window_s=3,
                                 fee_bps=100, creator_tax_bps=0, activity=_act())
    assert "inside launch snipe-tax window" in snipe.blockers
    dev = screen_almost_bonded(settings, token="0x1", progress=0.95, phase=0, launch_age_s=600, snipe_window_s=3,
                               fee_bps=100, creator_tax_bps=0, activity=_act(deployer_buy_share=0.5))
    assert any("deployer" in b for b in dev.blockers)
    tax = screen_almost_bonded(settings, token="0x1", progress=0.95, phase=0, launch_age_s=600, snipe_window_s=3,
                               fee_bps=100, creator_tax_bps=1000, activity=_act())
    assert any("fee+tax" in b for b in tax.blockers)


def test_screen_migrated_rules(settings):
    good = screen_migrated(settings, token="0x1", phase=2, pool_found=True, volume_h1_usd=30_000,
                           liquidity_usd=45_000, volume_h24_usd=240_000, fee_bps=100, creator_tax_bps=0, activity=None)
    assert good.eligible and good.metrics["volume_heat"] == 3.0
    for kw, msg in [({"phase": 1}, "phase"), ({"pool_found": False}, "genuine"),
                    ({"volume_h1_usd": 4_000}, "1h volume"), ({"liquidity_usd": 10_000}, "liquidity")]:
        args = dict(token="0x1", phase=2, pool_found=True, volume_h1_usd=30_000, liquidity_usd=45_000,
                    volume_h24_usd=240_000, fee_bps=100, creator_tax_bps=0, activity=None)
        args.update(kw)
        assert any(msg in b for b in screen_migrated(settings, **args).blockers)


def test_activity_requires_acceleration_across_windows(db):
    from ct_agent.db import utcnow
    db.execute("INSERT INTO launches(token,curve,deployer,pair_token,launch_config_id,graduation_threshold,"
               "block_number,tx_hash,first_seen) VALUES('0xT','0xC','0xD','0x0',0,'1',1,'0x',?)", (utcnow(),))
    head = 100_000
    n = 0
    for blk in [head - 30_000, head - 20_000, head - 5_000, head - 1_000, head - 500, head - 200, head - 100]:
        n += 1
        db.execute("INSERT INTO curve_trades VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                   (f"0x{n}", 0, "0xT", "0xC", blk, "buy", f"0xb{n}", str(ETH // 10), "1", "0", "0"))
    a = activity_for(db, "0xT", "0xD", head, 0.1, [300, 900, 3600])
    assert a.windows[0].buys == 4 and a.accelerating
