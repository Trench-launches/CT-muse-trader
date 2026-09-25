"""Launchpad: requests on X are checked and screened, approved ones launched, and each coin's creator fees handed
to the requester once they've paid the launch back."""
import json
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
                               "description": "a cat that trades stocks", "twitter": "https://x.com/catcoin", **kw})

    def review_launch(self, text):
        return MuseResult(self.d, "", "m", "h")


def _ctx(settings, db, brain=None, **kw):
    s = settings.model_copy(update={"launchpad_enabled": True, **kw})
    return SimpleNamespace(settings=s, conn=db, brain=brain or _Brain(), pons=None, rpc=None)


def _m(i="1", text=f"@MooseByMuse launch $CAT Cat Coin, a cat that trades stocks. fees to {WALLET}", images=("https://pbs.twimg.com/a.jpg",),
       author=None):
    return {"id": i, "author_id": "u1", "text": text, "images": list(images),
            "author": author or {"username": "catfan", "created_at": OLD}}


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
    def __init__(self, pending=0, swept=0, fail=False):
        self.pending, self.swept, self.fail, self.calls = pending, swept, fail, []

    def launch(self, **kw):
        self.calls.append(("launch", kw["symbol"]))
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
    assert launchpad_once(ctx, lp) == {"launched": 1, "handed_over": 0}
    assert "Launched $CAT for you" in _replies(db)[-1] and ("0x" + "c0" * 20) in _replies(db)[-1]
    lp.pending, lp.swept = 700_000_000_000_000, 700_000_000_000_000  # now the fees cover the launch
    assert launchpad_once(ctx, lp)["handed_over"] == 1
    assert lp.calls[-2:] == [("sweep", "0x" + "cc" * 20), ("hand_over", WALLET)]
    r = rows(db, "SELECT * FROM launch_requests")[0]
    assert r["status"] == "handed_over" and int(r["earned_wei"]) >= int(r["cost_wei"])
    assert "creator fees go straight to your wallet" in _replies(db)[-1]


def test_a_graduated_coin_is_handed_over_and_a_failed_launch_says_sorry(settings, db):
    ctx = _ctx(settings, db)
    take_request(ctx, _m("12"))
    ctx.pons = SimpleNamespace(launch_record=lambda t: SimpleNamespace(phase=Phase.POOL_CREATED))
    lp = _Launcher()
    launchpad_once(ctx, lp)
    assert ("hand_over", WALLET) in lp.calls and ("sweep", "0x" + "cc" * 20) not in lp.calls
    take_request(ctx, {**_m("13", text=f"launch $DOG Dog Coin {WALLET}"), "author_id": "u3"})
    ctx.brain = _Brain(symbol="DOG", name="Dog Coin")
    take_request(ctx, {**_m("14", text=f"launch $DOG Dog Coin {WALLET}"), "author_id": "u3"})
    launchpad_once(ctx, _Launcher(fail=True))
    assert rows(db, "SELECT status FROM launch_requests WHERE x_post_id='14'")[0]["status"] == "failed"
    assert "something went wrong on my side" in _replies(db)[-1]


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
