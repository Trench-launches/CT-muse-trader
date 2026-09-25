"""Launchpad: requests on X are checked and screened, approved ones launched, and each coin's creator fees handed
to the requester once they've paid the launch back."""
import json
import re
from types import SimpleNamespace

from eth_abi import decode

from ct_agent.brain.muse import MuseLaunch, MuseResult
from ct_agent.chain.pons import Phase
from ct_agent.db import rows
from ct_agent.launchpad.chain import TOKEN_PARAMS, LaunchBlocked, Launched, Launcher
from ct_agent.launchpad.service import launchpad_once, take_request

WALLET = "0x" + "ab" * 20
OLD = "2020-01-01T00:00:00.000Z"


class _Brain:
    def __init__(self, **kw):
        self.d = MuseLaunch(**{"is_request": True, "approve": True, "name": "Cat Coin", "symbol": "CAT",
                               "description": "a cat that trades stocks.", "twitter": "https://x.com/catcoin", **kw})

    def review_launch(self, text):
        return MuseResult(self.d, "", "m", "h")


def _ctx(settings, db, brain=None, **kw):
    s = settings.model_copy(update={"launchpad_enabled": True, **kw})
    return SimpleNamespace(settings=s, conn=db, brain=brain or _Brain(), pons=None, rpc=None)


def _m(i="1", text=f"@MooseByMuse launch $CAT Cat Coin, a cat that trades stocks. fees to {WALLET}", images=("https://pbs.twimg.com/a.jpg",),
       author=None):
    return {"id": i, "author_id": "u1", "text": text, "images": list(images),
            "author": author or {"username": "catfan", "created_at": OLD}}


def _announced(db, xid="900"):
    """The announcement went out on X (the social loop does this within seconds in the bot)."""
    db.execute("UPDATE posts SET status='sent', x_post_id=? WHERE kind='launch_announce' AND status='pending'", (xid,))


def _replies(db):
    return [r["body"] for r in rows(db, "SELECT body FROM posts ORDER BY id")]


def test_a_good_request_is_approved_with_the_image_and_the_fee_wallet(settings, db):
    assert take_request(_ctx(settings, db), _m()) == "approved"
    r = rows(db, "SELECT * FROM launch_requests")[0]
    assert (r["status"], r["name"], r["symbol"], r["recipient"], r["logo"]) == \
        ("approved", "Cat Coin", "CAT", WALLET, "https://pbs.twimg.com/a.jpg")
    assert json.loads(r["socials"]) == {"twitter": "https://x.com/catcoin"}
    assert take_request(_ctx(settings, db), _m()) == "seen" and _replies(db) == []  # same post twice: once


def test_refusals_each_get_a_short_reply(settings, db):
    cases = [(_m("2", text="launch $CAT Cat Coin"), "wallet"),
             (_m("3", images=()), "Attach an image"),
             (_m("4", author={"username": "new", "created_at": "2099-01-01T00:00:00Z"}), "older than 30 days")]
    for m, words in cases:
        assert take_request(_ctx(settings, db), m) == "refused"
        assert words in _replies(db)[-1]
    assert take_request(_ctx(settings, db, _Brain(approve=False, reason="I can't launch coins named after real brands.")),
                        _m("5")) == "refused" and _replies(db)[-1] == "I can't launch coins named after real brands."
    # Muse's name must really be in the post: no launching something they didn't ask for
    assert take_request(_ctx(settings, db, _Brain(symbol="DOG")), _m("6")) == "refused"


def test_one_launch_per_account_a_day_and_a_daily_budget(settings, db):
    assert take_request(_ctx(settings, db), _m("7")) == "approved"
    assert take_request(_ctx(settings, db, _Brain(symbol="CATT", name="Cat Coin")),
                        _m("8", text=f"launch $CATT Cat Coin {WALLET}")) == "refused"
    assert "One launch per account per day" in _replies(db)[-1]
    tiny = _ctx(settings, db, launch_daily_cap_eth=0.0001)
    assert take_request(tiny, {**_m("9", text=f"launch $DOGE Dog Coin {WALLET}"), "author_id": "u2"}) == "refused"


def test_not_a_request_falls_through_to_a_normal_reply(settings, db):
    assert take_request(_ctx(settings, db, _Brain(is_request=False)), _m("10", text="gm, when launch?")) == "not_request"
    assert rows(db, "SELECT * FROM launch_requests") == []


class _Launcher:
    wallet = "0x" + "99" * 20  # the launcher wallet: not the requester's

    def __init__(self, pending=0, swept=0, fail=False):
        self.pending, self.swept, self.fail, self.calls = pending, swept, fail, []

    def launch(self, **kw):
        self.calls.append(("launch", kw["symbol"]))
        self.last = kw
        if self.fail:
            raise LaunchBlocked("launcher wallet is low on ETH")
        return Launched("0x" + "c0" * 20, "0x" + "cc" * 20, "0xtx", 650_000_000_000_000)

    def pending_creator_wei(self, curve):
        return self.pending

    def sweep(self, curve, ref):
        self.calls.append(("sweep", curve))
        return self.swept

    def hand_over(self, token, to):
        self.calls.append(("hand_over", to))
        return "0xhand"

    def escrow_balance(self):
        return 0


def test_launch_replies_with_the_address_then_hands_over_once_the_cost_is_back(settings, db):
    ctx = _ctx(settings, db)
    take_request(ctx, _m("11"))
    ctx.pons = SimpleNamespace(launch_record=lambda t: SimpleNamespace(phase=Phase.NOT_GRADUATED))
    lp = _Launcher(pending=100)  # fees so far: not enough yet
    assert launchpad_once(ctx, lp) == {"launched": 0, "handed_over": 0}  # first the announcement, crediting them
    ann = rows(db, "SELECT * FROM posts WHERE kind='launch_announce'")[0]
    assert ann["body"].startswith("🚀 New on the MOOSE launchpad\n\n$CAT · Cat Coin\nCreator: @catfan") and ann["reply_to_key"] is None and lp.calls == []
    assert launchpad_once(ctx, lp)["launched"] == 0  # not launched until the announcement is out
    _announced(db)
    assert launchpad_once(ctx, lp) == {"launched": 1, "handed_over": 0}
    assert lp.last["socials"]["twitter"] == "https://x.com/catcoin"  # they gave their own link: it stays
    assert lp.last["description"] == "a cat that trades stocks. Created by @catfan, launched via @MooseByMuse"  # one full stop
    receipt = rows(db, "SELECT * FROM posts WHERE kind='launch_receipt'")[0]
    assert receipt["reply_to_key"] == ann["idempotency_key"] and "/tx/0xtx" in receipt["body"]
    assert ("0x" + "c0" * 20) in receipt["body"] and "paid by MOOSE" in receipt["body"]
    assert _replies(db)[-1].startswith("🚀 $CAT is live") and "x.com/MooseByMuse/status/900" in _replies(db)[-1]
    lp.pending, lp.swept = 700_000_000_000_000, 700_000_000_000_000  # now the fees cover the launch
    assert launchpad_once(ctx, lp)["handed_over"] == 1
    assert lp.calls[-2:] == [("sweep", "0x" + "cc" * 20), ("hand_over", WALLET)]
    r = rows(db, "SELECT * FROM launch_requests")[0]
    assert r["status"] == "handed_over" and int(r["earned_wei"]) >= int(r["cost_wei"])
    assert "creator fees go straight to your wallet" in _replies(db)[-1]


def test_a_graduated_coin_is_handed_over_and_a_failed_launch_says_sorry(settings, db):
    ctx = _ctx(settings, db, _Brain(twitter=""))  # no link of their own: the coin links to MOOSE's announcement
    take_request(ctx, _m("12"))
    ctx.pons = SimpleNamespace(launch_record=lambda t: SimpleNamespace(phase=Phase.POOL_CREATED))
    lp = _Launcher()
    launchpad_once(ctx, lp)
    _announced(db, "901")
    launchpad_once(ctx, lp)
    assert lp.last["socials"]["twitter"] == "https://x.com/MooseByMuse/status/901"
    assert ("hand_over", WALLET) in lp.calls and ("sweep", "0x" + "cc" * 20) not in lp.calls
    take_request(ctx, {**_m("13", text=f"launch $DOG Dog Coin {WALLET}"), "author_id": "u3"})
    ctx.brain = _Brain(symbol="DOG", name="Dog Coin")
    take_request(ctx, {**_m("14", text=f"launch $DOG Dog Coin {WALLET}"), "author_id": "u3"})
    launchpad_once(ctx, _Launcher(fail=True))
    _announced(db, "902")
    launchpad_once(ctx, _Launcher(fail=True))
    assert rows(db, "SELECT status FROM launch_requests WHERE x_post_id='14'")[0]["status"] == "failed"
    assert "something went wrong on my side" in _replies(db)[-1]
    sorry = rows(db, "SELECT body FROM posts WHERE kind='launch_receipt' AND body LIKE '%didn''t launch%'")
    assert sorry and sorry[0]["body"].startswith("❌ $DOG didn't launch") and "no coin was made" in sorry[0]["body"]  # the announcement never points at nothing


def test_launch_calldata_carries_the_coin_and_makes_moose_the_fee_recipient(settings, db):
    s = settings.model_copy(update={"launcher_wallet_private_key": "0x" + "22" * 32})
    lp = Launcher(s, None, None, db)
    data = lp.launch_calldata(name="Cat Coin", symbol="CAT", logo="https://pbs.twimg.com/a.jpg", description="d",
                              socials={"twitter": "https://x.com/catcoin"}, economics=b"\1" * 32, salt=b"\2" * 32)
    assert data.startswith("0xf35abbcf")  # launchToken(TokenParams,uint256,address), as in the verified factory
    params, cfg, pair = decode([TOKEN_PARAMS, "uint256", "address"], bytes.fromhex(data[10:]))
    assert params[:4] == ("Cat Coin", "CAT", "https://pbs.twimg.com/a.jpg", "d")
    assert params[4] == ("https://x.com/catcoin", "", "", "", "")
    assert params[5].lower() == lp.wallet.lower() and params[6] == 200 and params[7] is False
    assert (cfg, pair) == (0, "0x0000000000000000000000000000000000000000")


def test_the_site_lists_recent_launches_with_their_proof(settings, db):
    from ct_agent.api.site import _CACHE, site_data
    ctx = _ctx(settings, db)
    take_request(ctx, _m("20"))
    ctx.pons = SimpleNamespace(launch_record=lambda t: SimpleNamespace(phase=Phase.NOT_GRADUATED))
    launchpad_once(ctx, _Launcher())
    _announced(db, "950")
    launchpad_once(ctx, _Launcher())
    _CACHE.clear()
    lp = site_data(db, ctx.settings)["launchpad"]
    assert lp["enabled"] is False  # no launcher key in the test settings: the site keeps the button closed
    r = lp["recent"][0]
    assert (r["symbol"], r["handle"], r["announce_x_id"], r["tx_hash"]) == ("CAT", "catfan", "950", "0xtx")
    assert r["token"] == "0x" + "c0" * 20 and r["fees_handed_over"] is False


def test_fees_pointed_at_the_launcher_stay_there_and_sweeps_stop_once_repaid(settings, db):
    ctx = _ctx(settings, db, _Brain(twitter=""))
    lw = "0x" + "77" * 20
    take_request(ctx, _m("30", text=f"@MooseByMuse launch $CAT Cat Coin fees to {lw}"))
    ctx.pons = SimpleNamespace(launch_record=lambda t: SimpleNamespace(phase=Phase.NOT_GRADUATED))
    lp = _Launcher(pending=10**15, swept=10**15)
    lp.wallet = lw
    launchpad_once(ctx, lp)
    _announced(db, "960")
    launchpad_once(ctx, lp)  # launch
    launchpad_once(ctx, lp)  # repaid: one sweep, and no hand-over to itself
    launchpad_once(ctx, lp)
    assert [c for c in lp.calls if c[0] == "sweep"] == [("sweep", "0x" + "cc" * 20)]
    assert not [c for c in lp.calls if c[0] == "hand_over"]
    assert rows(db, "SELECT status FROM launch_requests")[0]["status"] == "handed_over"


def test_a_retry_after_a_refusal_also_answers_under_the_first_post(settings, db):
    ctx = _ctx(settings, db)
    assert take_request(ctx, _m("40", images=())) == "refused"  # forgot the image
    assert take_request(ctx, _m("41")) == "approved"            # asked again, properly
    ctx.pons = SimpleNamespace(launch_record=lambda t: SimpleNamespace(phase=Phase.NOT_GRADUATED))
    launchpad_once(ctx, _Launcher())
    _announced(db, "970")
    launchpad_once(ctx, _Launcher())
    under_first = rows(db, "SELECT body FROM posts WHERE reply_to_key='x:40' AND kind='launch_followup'")
    assert under_first and under_first[0]["body"].startswith("🚀 $CAT is live now")
    assert "x.com/MooseByMuse/status/970" in under_first[0]["body"] and ("0x" + "c0" * 20) in under_first[0]["body"]


def test_the_website_can_follow_a_launch_request(settings, db, tmp_path):
    from fastapi.testclient import TestClient
    from ct_agent.api.app import create_app
    s = settings.model_copy(update={"launchpad_enabled": True})
    ctx = _ctx(settings, db)
    api = TestClient(create_app(s))
    assert api.get("/launchpad/status", params={"symbol": "CAT"}).json() == {"status": "not_seen"}
    take_request(ctx, _m("50"))
    assert api.get("/launchpad/status", params={"symbol": "$cat"}).json()["status"] == "approved"
    ctx.pons = SimpleNamespace(launch_record=lambda t: SimpleNamespace(phase=Phase.NOT_GRADUATED))
    launchpad_once(ctx, _Launcher())
    _announced(db, "980")
    launchpad_once(ctx, _Launcher())
    st = api.get("/launchpad/status", params={"symbol": "CAT"}).json()
    assert (st["status"], st["token"], st["announce_x_id"], st["handle"]) == ("launched", "0x" + "c0" * 20, "980", "catfan")


def test_any_name_goes_even_a_ticker_that_exists_but_not_moose(settings, db):
    from fastapi.testclient import TestClient
    from ct_agent.api.app import create_app
    ctx = _ctx(settings, db)
    other = "0x" + "cd" * 20
    assert take_request(ctx, _m("60")) == "approved"
    assert take_request(ctx, {**_m("61", text=f"@MooseByMuse launch $CAT Cat Coin fees to {other}"), "author_id": "u9"}) \
        == "approved"  # a second $CAT from someone else is fine
    api = TestClient(create_app(ctx.settings))
    assert api.get("/launchpad/status", params={"symbol": "CAT", "wallet": WALLET}).json()["status"] == "approved"
    mine = api.get("/launchpad/status", params={"symbol": "CAT", "wallet": other.upper()}).json()
    assert mine["status"] == "approved" and mine["handle"] == "catfan"
    moose = _ctx(settings, db, _Brain(symbol="MOOSE", name="Moose Two"))
    assert take_request(moose, {**_m("62", text=f"launch $MOOSE Moose Two {WALLET}"), "author_id": "u8"}) == "refused"
    assert "$MOOSE is my own coin" in _replies(db)[-1]


TSLA = "0x322F0929c4625eD5bAd873c95208D54E1c003b2d"


def test_a_stock_paired_coin_pays_its_creator_from_the_start(settings, db):
    ctx = _ctx(settings, db)
    assert take_request(ctx, _m("70", text=f"@MooseByMuse launch $CAT Cat Coin\n\npaired with $tsla\n\nfees to {WALLET}")) \
        == "approved"
    r = rows(db, "SELECT * FROM launch_requests")[0]
    assert (r["symbol"], r["pair_symbol"], r["pair_token"]) == ("CAT", "TSLA", TSLA)
    ctx.pons = SimpleNamespace(launch_record=lambda t: SimpleNamespace(phase=Phase.NOT_GRADUATED))
    lp = _Launcher(pending=10**18, swept=10**18)
    launchpad_once(ctx, lp)
    assert "Paired with TSLA" in rows(db, "SELECT body FROM posts WHERE kind='launch_announce'")[0]["body"]
    _announced(db, "990")
    launchpad_once(ctx, lp)
    assert (lp.last["pair_token"], lp.last["recipient"]) == (TSLA, WALLET)  # the creator is the fee recipient at launch
    r = rows(db, "SELECT * FROM launch_requests")[0]
    assert r["status"] == "handed_over" and r["handed_at"]
    receipt = rows(db, "SELECT body FROM posts WHERE kind='launch_receipt'")[0]["body"]
    assert "Pair: TSLA" in receipt and "straight to @catfan, paid in TSLA" in receipt
    assert "paid in TSLA" in _replies(db)[-1]
    for body in _replies(db):  # X refuses a post with more than one cashtag
        assert len(set(re.findall(r"\$[A-Za-z][A-Za-z0-9]*", body))) <= 1, body
    launchpad_once(ctx, lp)  # nothing left for MOOSE to do: no sweeps, no hand-over
    assert [c[0] for c in lp.calls] == ["launch"]


def test_eth_launches_pass_no_pair_and_an_unknown_pair_is_refused(settings, db):
    ctx = _ctx(settings, db)
    assert take_request(ctx, _m("71", text=f"launch $CAT Cat Coin pair: ETH, a pair of socks {WALLET}")) == "approved"
    assert rows(db, "SELECT pair_symbol FROM launch_requests")[0]["pair_symbol"] is None
    assert take_request(ctx, {**_m("72", text=f"launch $CAT Cat Coin pair: DOGE {WALLET}"), "author_id": "u5"}) == "refused"
    assert "not $DOGE" in _replies(db)[-1]
    ctx.pons = SimpleNamespace(launch_record=lambda t: SimpleNamespace(phase=Phase.NOT_GRADUATED))
    lp = _Launcher()
    launchpad_once(ctx, lp)
    _announced(db, "991")
    launchpad_once(ctx, lp)
    assert lp.last["pair_token"] == "0x0000000000000000000000000000000000000000" and lp.last["recipient"] is None


def test_launch_calldata_for_a_stock_pair_names_the_pair_and_the_creator(settings, db):
    s = settings.model_copy(update={"launcher_wallet_private_key": "0x" + "22" * 32})
    lp = Launcher(s, None, None, db)
    data = lp.launch_calldata(name="Cat Coin", symbol="CAT", logo="l", description="d", socials={},
                              economics=b"\1" * 32, salt=b"\2" * 32, pair_token=TSLA, recipient=WALLET)
    params, cfg, pair = decode([TOKEN_PARAMS, "uint256", "address"], bytes.fromhex(data[10:]))
    assert params[5].lower() == WALLET and (cfg, pair.lower()) == (0, TSLA.lower())
