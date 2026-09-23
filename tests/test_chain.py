from ct_agent.chain.pons import Phase, PonsV2
from ct_agent.chain.rpc import RpcClient

from .fakes import FACTORY, FakeChain, FakeToken, addr


def make(chain: FakeChain) -> PonsV2:
    return PonsV2(RpcClient("http://fake", transport=chain.transport(), retries=0), FACTORY, log_chunk=100)


def test_launch_events_and_record_decode():
    ch = FakeChain()
    t = FakeToken(token=addr(0xA1), curve=addr(0xC1), deployer=addr(0xD1), creator_tax_bps=400)
    ch.launch(t, 999_950)
    p = make(ch)
    ev = p.launches_between(999_000, 1_000_000)  # spans several 100-block chunks
    assert len(ev) == 1 and ev[0].token == t.token and ev[0].curve == t.curve
    assert ev[0].graduation_threshold == 4_200_000_000_000_000_000
    rec = p.launch_record(t.token)
    assert rec.exists and rec.phase is Phase.NOT_GRADUATED and rec.creator_tax_bps == 400 and rec.native_quote


def test_curve_progress_and_trades():
    ch = FakeChain()
    t = FakeToken(token=addr(0xA1), curve=addr(0xC1), deployer=addr(0xD1))
    ch.launch(t, 999_000)
    ch.trade(t, 999_500, "buy", 3_000_000_000_000_000_000, 10**24, addr(0xB1))
    ch.trade(t, 999_600, "sell", 100_000_000_000_000_000, 10**22, addr(0xB2))
    p = make(ch)
    s = p.curve_state(t.curve)
    assert 0.6 < s.progress < 0.72
    trades = p.curve_trades([t.curve], 999_000, 1_000_000)
    assert [x.side for x in trades] == ["buy", "sell"]
    assert trades[0].quote_amount == 3 * 10**18 and trades[1].token_amount == 10**22


def test_verify_rejects_unknown_and_accepts_launch():
    ch = FakeChain()
    t = FakeToken(token=addr(0xA1), curve=addr(0xC1), deployer=addr(0xD1))
    ch.launch(t, 999_000)
    p = make(ch)
    assert p.verify_token(t.token)[0] is True
    ok, why, _ = p.verify_token(addr(0xFFFF))
    assert not ok and "no contract code" in why
    assert p.verify_token("not-an-address")[0] is False


def test_genuine_pool_id_matches_known_mainnet_pool():
    # Values observed on mainnet for token 0x3967...e751 (see research notes).
    ch = FakeChain()
    t = FakeToken(token="0x39679b59289CcE7C7f6e0fEacDF39A1e543dE751", curve=addr(0xC1), deployer=addr(0xD1),
                  pool_fee=0, tick_spacing=200, phase=2)
    ch.launch(t, 1)
    p = make(ch)
    assert p.genuine_pool_id(p.launch_record(t.token)) == \
        "0x6c96006423d90e4aca950b72e50b143db065917b28537c7ca8fe7cd7d6e30022"
