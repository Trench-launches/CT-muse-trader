"""Launchpad on pump.fun: requests on X are checked and screened, approved ones announced then launched, and each
coin's creator fees split once (90% requester / 10% VIGIL). pump.fun, PumpPortal, Pinata and the Solana node are
offline fakes that speak the real wire formats (tests/launchpad_fakes.py)."""
import base64
import hashlib
import json
import struct
from types import SimpleNamespace

import base58
import pytest
from solders.keypair import Keypair
from solders.pubkey import Pubkey

from ct_agent.brain.grok import GrokLaunch, GrokResult
from ct_agent.db import journal_recent, kv_set, rows
from ct_agent.launchpad import pumpfun as pf
from ct_agent.launchpad.pumpfun import (FeeSplitUnverified, LaunchBlocked, LaunchResult, LaunchUnconfirmed,
                                        PumpLauncher, Share)
from ct_agent.launchpad.service import launchpad_once, take_request

from .launchpad_fakes import CREATE_COST, FakeSolana, FakeWeb, config_bytes, curve_bytes

REQUESTER = Keypair.from_seed(bytes([1] * 32)).pubkey()
WALLET = str(REQUESTER)                      # base58, mixed case: must be stored exactly as written
LAUNCHER_KP = Keypair.from_seed(bytes([2] * 32))
LAUNCHER = str(LAUNCHER_KP.pubkey())
LAUNCHER_SECRET = base58.b58encode(bytes(LAUNCHER_KP)).decode()
OLD = "2020-01-01T00:00:00.000Z"
MINT = str(Keypair.from_seed(bytes([3] * 32)).pubkey())


class _Brain:
    def __init__(self, **kw):
        self.d = GrokLaunch(**{"is_request": True, "approve": True, "name": "Cat Coin", "symbol": "CAT",
                               "description": "a cat that trades memes.", "twitter": "https://x.com/catcoin", **kw})

    def review_launch(self, text):
        return GrokResult(self.d, "", "m", "h")


def _ctx(settings, db, brain=None, rpc=None, **kw):
    s = settings.model_copy(update={"launchpad_enabled": True, "x_handle": "VigilBot", **kw})
    return SimpleNamespace(settings=s, conn=db, brain=brain or _Brain(), rpc=rpc, web=None)


def _m(i="1", text=f"@VigilBot launch $CAT Cat Coin, a cat that trades memes. fees to {WALLET}",
       images=("https://pbs.twimg.com/a.jpg",), author=None):
    return {"id": i, "author_id": "u1", "text": text, "images": list(images),
            "author": author or {"username": "catfan", "created_at": OLD}}


def _announced(db, xid="900"):
    """The announcement went out on X (the social loop does this within seconds in the bot)."""
    db.execute("UPDATE posts SET status='sent', x_post_id=? WHERE kind='launch_announce' AND status='pending'", (xid,))


def _replies(db):
    return [r["body"] for r in rows(db, "SELECT body FROM posts ORDER BY id")]


# --- request rules ------------------------------------------------------------------------------------------------

def test_a_good_request_is_approved_with_the_image_and_the_exact_wallet(settings, db):
    assert take_request(_ctx(settings, db), _m()) == "approved"
    r = rows(db, "SELECT * FROM launch_requests")[0]
    assert (r["status"], r["name"], r["symbol"], r["recipient"], r["logo"]) == \
        ("approved", "Cat Coin", "CAT", WALLET, "https://pbs.twimg.com/a.jpg")  # case kept: base58 is case-sensitive
    assert json.loads(r["socials"]) == {"twitter": "https://x.com/catcoin"}
    assert take_request(_ctx(settings, db), _m()) == "seen" and _replies(db) == []  # same post twice: once


def test_refusals_each_get_a_short_reply(settings, db):
    pda = str(pf.bonding_curve_pda(MINT))  # off the curve: a token account or PDA is never someone's wallet
    other = str(Keypair.from_seed(bytes([9] * 32)).pubkey())
    cases = [(_m("2", text="launch $CAT Cat Coin"), "Add the Solana wallet that should get the creator fees"),
             (_m("2b", text=f"launch $CAT Cat Coin {WALLET} {other}"), "one address"),
             (_m("2c", text=f"launch $CAT Cat Coin {pda}"), "Solana wallet"),
             (_m("2d", text=f"launch $CAT Cat Coin {WALLET.lower()}"), "Solana wallet"),  # lowercased: not that wallet
             (_m("3", images=()), "Attach an image"),
             (_m("4", author={"username": "new", "created_at": "2099-01-01T00:00:00Z"}), "older than 30 days")]
    for m, words in cases:
        assert take_request(_ctx(settings, db), m) == "refused", m["text"]
        assert words in _replies(db)[-1]
    assert take_request(_ctx(settings, db, _Brain(approve=False, reason="I can't launch coins named after real brands.")),
                        _m("5")) == "refused" and _replies(db)[-1] == "I can't launch coins named after real brands."
    # Grok's name must really be in the post: no launching something they didn't ask for
    assert take_request(_ctx(settings, db, _Brain(symbol="DOG")), _m("6")) == "refused"


def test_program_ids_and_well_known_mints_are_not_wallets(settings, db):
    text = f"launch $CAT Cat Coin like So11111111111111111111111111111111111111112 fees to {WALLET}"
    assert take_request(_ctx(settings, db), _m("7", text=text)) == "approved"
    assert rows(db, "SELECT recipient FROM launch_requests")[0]["recipient"] == WALLET


def test_one_launch_per_account_a_day_and_a_daily_sol_budget(settings, db):
    assert take_request(_ctx(settings, db), _m("7")) == "approved"
    assert take_request(_ctx(settings, db, _Brain(symbol="CATT", name="Cat Coin")),
                        _m("8", text=f"launch $CATT Cat Coin {WALLET}")) == "refused"
    assert "One launch per account per day" in _replies(db)[-1]
    tiny = _ctx(settings, db, _Brain(symbol="DOGE", name="Dog Coin"), launch_daily_cap_sol=0.05)
    assert take_request(tiny, {**_m("9", text=f"launch $DOGE Dog Coin {WALLET}"), "author_id": "u2"}) == "refused"
    assert "Launches are full for today" in _replies(db)[-1]
    # what launched in the last 24h counts at its real cost
    db.execute("UPDATE launch_requests SET status='launched', cost_lamports=?, launched_at=? WHERE x_post_id='7'",
               (str(10**9), "2999-01-01T00:00:00+00:00"))
    big = _ctx(settings, db, _Brain(symbol="DOGE", name="Dog Coin"), launch_daily_cap_sol=1.0)
    assert take_request(big, {**_m("10", text=f"launch $DOGE Dog Coin {WALLET}"), "author_id": "u3"}) == "refused"


def test_not_a_request_falls_through_to_a_normal_reply(settings, db):
    assert take_request(_ctx(settings, db, _Brain(is_request=False)), _m("10", text="gm, when launch?")) == "not_request"
    assert rows(db, "SELECT * FROM launch_requests") == []


def test_a_vip_handle_gets_more_launches_a_day(settings, db):
    ctx = _ctx(settings, db, launch_vip_handles=["@WilsTheDev"], launch_vip_per_day=3)
    vip = {"username": "wilsthedev", "created_at": OLD}
    for i, sym in enumerate(("CAT", "CATT", "CATTT")):
        ctx.brain = _Brain(symbol=sym, name="Cat Coin")
        assert take_request(ctx, _m(f"8{i}", text=f"launch ${sym} Cat Coin {WALLET}", author=vip)) == "approved"
    ctx.brain = _Brain(symbol="CATS", name="Cat Coin")
    assert take_request(ctx, _m("89", text=f"launch $CATS Cat Coin {WALLET}", author=vip)) == "refused"  # 4th
    assert take_request(ctx, {**_m("90", text=f"launch $CATS Cat Coin {WALLET}"), "author_id": "u7"}) == "approved"


def test_any_name_goes_except_our_own_ticker(settings, db):
    ctx = _ctx(settings, db)
    other = str(Keypair.from_seed(bytes([8] * 32)).pubkey())
    assert take_request(ctx, _m("60")) == "approved"
    assert take_request(ctx, {**_m("61", text=f"@VigilBot launch $CAT Cat Coin fees to {other}"), "author_id": "u9"}) \
        == "approved"  # a second $CAT from someone else is fine
    ours = _ctx(settings, db, _Brain(symbol="VIGIL", name="Vigil Two"))
    assert take_request(ours, {**_m("62", text=f"launch $VIGIL Vigil Two {WALLET}"), "author_id": "u8"}) == "refused"
    assert "$VIGIL is my own coin" in _replies(db)[-1]


# --- the launch loop (with a stand-in launcher) --------------------------------------------------------------------

class _Launcher:
    wallet = LAUNCHER

    def __init__(self, fail=None, split_error=None, unconfirmed=False):
        self.fail, self.split_error, self.unconfirmed = fail, split_error, unconfirmed
        self.calls, self.last = [], {}

    def upload_metadata(self, name, symbol, description, image_url, socials=None):
        self.calls.append(("upload", symbol))
        self.last.update(description=description, socials=dict(socials or {}), image=image_url)
        return "https://ipfs.io/ipfs/bafymeta"

    def launch(self, name, symbol, uri, dev_buy_sol, on_signed=None):
        self.calls.append(("launch", symbol))
        if self.fail:
            raise LaunchBlocked(self.fail)
        if on_signed:
            on_signed(MINT, "SIG1")
        if self.unconfirmed:
            raise LaunchUnconfirmed("not confirmed within 60s", MINT, "SIG1")
        return LaunchResult(MINT, "SIG1", 23_400_000)

    def set_fee_split(self, mint, shares):
        self.calls.append(("split", [(s.address, s.bps) for s in shares]))
        if self.split_error:
            raise self.split_error
        return "SPLITSIG"

    def distribute_fees(self, mint):
        self.calls.append(("distribute", mint))
        return None

    def tx_cost(self, sig):
        return 22_000_000


def test_announce_then_launch_then_split_90_10(settings, db):
    ctx = _ctx(settings, db)
    take_request(ctx, _m("11"))
    lp = _Launcher()
    assert launchpad_once(ctx, lp)["launched"] == 0  # first the announcement, crediting them
    ann = rows(db, "SELECT * FROM posts WHERE kind='launch_announce'")[0]
    assert ann["body"].startswith("🚀 New on the VIGIL launchpad\n\n$CAT · Cat Coin\nCreator: @catfan")
    assert "pump.fun" in ann["body"] and ann["reply_to_key"] is None and lp.calls == []
    assert launchpad_once(ctx, lp)["launched"] == 0  # not launched until the announcement is out
    _announced(db)
    assert launchpad_once(ctx, lp) == {"launched": 1, "fee_splits": 0, "distributed": 0}
    assert lp.last["socials"]["twitter"] == "https://x.com/catcoin"  # they gave their own link: it stays
    assert lp.last["description"] == "a cat that trades memes. Created by @catfan, launched via @VigilBot"
    assert ("split", [(WALLET, 9000), (LAUNCHER, 1000)]) in lp.calls  # requester first, then VIGIL
    r = rows(db, "SELECT * FROM launch_requests")[0]
    assert (r["status"], r["token"], r["tx_hash"], r["cost_lamports"], r["fee_share_tx"]) == \
        ("launched", MINT, "SIG1", "23400000", "SPLITSIG")
    assert json.loads(r["fee_shares"]) == [{"address": WALLET, "bps": 9000}, {"address": LAUNCHER, "bps": 1000}]
    assert r["metadata_uri"] == "https://ipfs.io/ipfs/bafymeta"
    receipt = rows(db, "SELECT * FROM posts WHERE kind='launch_receipt'")[0]
    assert receipt["reply_to_key"] == ann["idempotency_key"]
    body = receipt["body"]
    assert "is live on Solana" in body and "https://solscan.io/tx/SIG1" in body and f"https://pump.fun/coin/{MINT}" in body
    assert "0.0234 SOL (paid by VIGIL)" in body and "90% to @catfan, 10% to VIGIL" in body
    live = next(b for b in _replies(db) if "is live on Solana\n\nCA:" in b and "x.com/i/status/900" in b)
    assert "90% of its creator fees go to your wallet" in live
    assert "https://solscan.io/tx/SPLITSIG" in _replies(db)[-1] and "locked" in _replies(db)[-1]
    assert launchpad_once(ctx, lp) == {"launched": 0, "fee_splits": 0, "distributed": 0}  # all done; payout throttled
    assert [c[0] for c in lp.calls].count("launch") == 1


def test_a_failed_launch_says_sorry_and_the_coin_links_to_the_announcement(settings, db):
    ctx = _ctx(settings, db, _Brain(twitter=""))  # no link of their own: the coin links to VIGIL's announcement
    take_request(ctx, _m("12"))
    lp = _Launcher()
    launchpad_once(ctx, lp)
    _announced(db, "901")
    launchpad_once(ctx, lp)
    assert lp.last["socials"]["twitter"] == "https://x.com/i/status/901"
    ctx.brain = _Brain(symbol="DOG", name="Dog Coin")
    take_request(ctx, {**_m("14", text=f"launch $DOG Dog Coin {WALLET}"), "author_id": "u3"})
    launchpad_once(ctx, _Launcher(fail="launcher wallet is low on SOL"))
    _announced(db, "902")
    launchpad_once(ctx, _Launcher(fail="launcher wallet is low on SOL"))
    r = rows(db, "SELECT * FROM launch_requests WHERE x_post_id='14'")[0]
    assert r["status"] == "failed" and "low on SOL" in r["reason"] and r["tx_hash"] is None
    assert "something went wrong on my side" in _replies(db)[-1]
    sorry = rows(db, "SELECT body FROM posts WHERE kind='launch_receipt' AND body LIKE '%didn''t launch%'")
    assert sorry and sorry[0]["body"].startswith("❌ $DOG didn't launch") and "no coin was made" in sorry[0]["body"]


def test_a_sent_but_unconfirmed_launch_is_never_sent_again(settings, db):
    node = FakeSolana()
    ctx = _ctx(settings, db, rpc=node.rpc)
    take_request(ctx, _m("15"))
    launchpad_once(ctx, _Launcher())
    _announced(db, "903")
    lp = _Launcher(unconfirmed=True)
    assert launchpad_once(ctx, lp)["launched"] == 0
    r = rows(db, "SELECT * FROM launch_requests")[0]
    assert (r["status"], r["token"], r["tx_hash"]) == ("launching", MINT, "SIG1")  # recorded before it went out
    launchpad_once(ctx, lp)  # the node hasn't seen it yet: wait, and don't launch again
    assert [c[0] for c in lp.calls].count("launch") == 1
    node.statuses["SIG1"] = {"confirmationStatus": "confirmed", "err": None}  # it landed after all
    assert launchpad_once(ctx, lp)["launched"] == 1
    r = rows(db, "SELECT * FROM launch_requests")[0]
    assert (r["status"], r["cost_lamports"], r["fee_share_tx"]) == ("launched", "22000000", "SPLITSIG")
    assert [c[0] for c in lp.calls].count("launch") == 1


def test_a_launch_that_never_lands_fails_without_a_relaunch(settings, db):
    node = FakeSolana()
    ctx = _ctx(settings, db, rpc=node.rpc)
    take_request(ctx, _m("16"))
    launchpad_once(ctx, _Launcher())
    _announced(db, "904")
    lp = _Launcher(unconfirmed=True)
    launchpad_once(ctx, lp)
    r = rows(db, "SELECT * FROM launch_requests")[0]
    kv_set(db, f"launch_signed_at:{r['id']}", 0)  # long ago: its blockhash has expired
    launchpad_once(ctx, lp)
    r = rows(db, "SELECT * FROM launch_requests")[0]
    assert r["status"] == "failed" and r["tx_hash"] == "SIG1"
    assert [c[0] for c in lp.calls].count("launch") == 1


def test_an_approved_row_with_a_tx_hash_is_not_launched_again(settings, db):
    node = FakeSolana()
    ctx = _ctx(settings, db, rpc=node.rpc)
    take_request(ctx, _m("17"))
    db.execute("UPDATE launch_requests SET tx_hash='SIG0', token=?", (MINT,))
    lp = _Launcher()
    launchpad_once(ctx, lp)
    launchpad_once(ctx, lp)
    assert not [c for c in lp.calls if c[0] == "launch"]


def test_an_unverified_fee_split_is_journalled_once_and_retried_later(settings, db):
    ctx = _ctx(settings, db)
    take_request(ctx, _m("18"))
    lp = _Launcher(split_error=FeeSplitUnverified("not verified", {"err": None, "logs": ["ok"]}))
    launchpad_once(ctx, lp)
    _announced(db, "905")
    launchpad_once(ctx, lp)
    launchpad_once(ctx, lp)
    r = rows(db, "SELECT * FROM launch_requests")[0]
    assert r["status"] == "launched" and r["fee_shares"] is None and r["fee_share_tx"] is None
    assert [c[0] for c in lp.calls].count("split") == 1  # backed off, not hammered every loop
    assert len(journal_recent(db, kind="launchpad_fee_split_unverified")) == 1
    kv_set(db, f"launch_split_next:{r['id']}", 0)
    lp.split_error = None
    assert launchpad_once(ctx, lp)["fee_splits"] == 1
    assert rows(db, "SELECT fee_share_tx FROM launch_requests")[0]["fee_share_tx"] == "SPLITSIG"


def test_a_retry_after_a_refusal_also_answers_under_the_first_post(settings, db):
    ctx = _ctx(settings, db)
    assert take_request(ctx, _m("40", images=())) == "refused"  # forgot the image
    assert take_request(ctx, _m("41")) == "approved"            # asked again, properly
    launchpad_once(ctx, _Launcher())
    _announced(db, "970")
    launchpad_once(ctx, _Launcher())
    under_first = rows(db, "SELECT body FROM posts WHERE reply_to_key='x:40' AND kind='launch_followup'")
    assert under_first and under_first[0]["body"].startswith("🚀 $CAT is live now")
    assert "x.com/i/status/970" in under_first[0]["body"] and MINT in under_first[0]["body"]


# --- PumpLauncher against the fakes ------------------------------------------------------------------------------------

def _launcher(settings, db, node, web, **kw):
    s = settings.model_copy(update={"launchpad_enabled": True, "launcher_wallet_private_key": LAUNCHER_SECRET,
                                    "pinata_jwt": "test-jwt", "launch_dev_buy_sol": 0.01, "x_handle": "VigilBot"})
    return PumpLauncher(s, node.rpc, db, transport=web.transport, **kw)


def _funded():
    node = FakeSolana()
    node.balances[LAUNCHER] = 10**9
    return node


def test_metadata_goes_to_pinata_as_image_then_json(settings, db):
    web = FakeWeb()
    lp = _launcher(settings, db, _funded(), web)
    uri = lp.upload_metadata("Cat Coin", "CAT", "a cat", "https://pbs.twimg.com/media/a.jpg",
                             {"twitter": "https://x.com/catcoin", "website": "https://cat.example"})
    assert uri == "https://ipfs.io/ipfs/bafymetadata2"
    assert [p["kind"] for p in web.pins] == ["image", "metadata"] and all(p["public"] for p in web.pins)
    assert web.pins[1]["json"] == {"name": "Cat Coin", "symbol": "CAT", "description": "a cat",
                                   "image": "https://ipfs.io/ipfs/bafyimage1", "twitter": "https://x.com/catcoin",
                                   "telegram": "", "website": "https://cat.example", "showName": True}
    with pytest.raises(LaunchBlocked, match="attached on X"):  # a post can't make the server fetch other hosts
        lp.upload_metadata("Cat Coin", "CAT", "a cat", "http://169.254.169.254/latest", {})


def test_launch_signs_with_the_mint_and_the_launcher_and_measures_the_cost(settings, db):
    node, web = _funded(), FakeWeb()
    lp = _launcher(settings, db, node, web)
    seen = []
    out = lp.launch("Cat Coin", "CAT", "https://ipfs.io/ipfs/bafymeta", 0.01, on_signed=lambda m, s: seen.append((m, s)))
    body = web.portal_requests[0]
    assert set(body) == {"publicKey", "action", "tokenMetadata", "mint", "denominatedInSol", "amount", "slippage",
                         "priorityFee", "pool"}  # the mint's public key only: its secret never leaves the process
    assert (body["publicKey"], body["action"], body["pool"], body["denominatedInSol"], body["amount"]) == \
        (LAUNCHER, "create", "pump", "true", 0.01)
    assert body["tokenMetadata"] == {"name": "Cat Coin", "symbol": "CAT", "uri": "https://ipfs.io/ipfs/bafymeta"}
    tx = node.sent[0]  # the node checked both signatures
    assert len(node.sent) == 1 and len(tx.signatures) == 2 and all(tx.verify_with_results())
    assert {str(k) for k in list(tx.message.account_keys)[:2]} == {LAUNCHER, body["mint"]}
    assert out.mint == body["mint"] and out.signature == str(tx.signatures[0]) and out.cost_lamports == CREATE_COST
    assert seen == [(out.mint, out.signature)]
    assert journal_recent(db, kind="launchpad_launched")


@pytest.mark.parametrize("trap, words", [("extra_signer", "other signers"), ("extra_program", "unexpected program"),
                                         ("portal_status", "HTTP 400")])
def test_a_suspicious_or_failed_pumpportal_answer_is_never_signed(settings, db, trap, words):
    node, web = _funded(), FakeWeb()
    if trap == "extra_signer":
        web.extra_signer = Keypair()
    elif trap == "extra_program":
        web.extra_program = Pubkey.new_unique()
    else:
        web.portal_status = 400
    with pytest.raises(LaunchBlocked, match=words):
        _launcher(settings, db, node, web).launch("Cat Coin", "CAT", "uri", 0.01)
    assert node.sent == []


def test_launch_refuses_when_low_on_sol_or_the_simulation_fails(settings, db):
    node, web = FakeSolana(), FakeWeb()
    node.balances[LAUNCHER] = 10_000_000
    with pytest.raises(LaunchBlocked, match="low on SOL"):
        _launcher(settings, db, node, web).launch("Cat Coin", "CAT", "uri", 0.01)
    node.balances[LAUNCHER] = 10**9
    node.sim_err = {"InstructionError": [0, {"Custom": 6000}]}
    with pytest.raises(LaunchBlocked, match="simulation failed"):
        _launcher(settings, db, node, web).launch("Cat Coin", "CAT", "uri", 0.01)
    assert node.sent == []


def test_a_launch_that_fails_on_chain_is_reported_not_retried(settings, db):
    node, web = _funded(), FakeWeb()
    node.fail_on_chain = {"InstructionError": [1, {"Custom": 1}]}
    with pytest.raises(LaunchBlocked) as e:
        _launcher(settings, db, node, web).launch("Cat Coin", "CAT", "uri", 0.01)
    assert not isinstance(e.value, LaunchUnconfirmed)


# --- the fee split -----------------------------------------------------------------------------------------------------

def test_discriminators_are_anchors_global_hashes():
    for name, disc in [("create_fee_sharing_config", pf.DISC_CREATE_FEE_SHARING_CONFIG),
                       ("update_fee_shares_v2", pf.DISC_UPDATE_FEE_SHARES_V2),
                       ("sweep_creator_fee", pf.DISC_SWEEP_CREATOR_FEE),
                       ("distribute_creator_fees_v2", pf.DISC_DISTRIBUTE_CREATOR_FEES_V2)]:
        assert disc == hashlib.sha256(f"global:{name}".encode()).digest()[:8] == pf.discriminator(name)
    assert pf.DISC_SHARING_CONFIG == hashlib.sha256(b"account:SharingConfig").digest()[:8]
    assert pf.DISC_BONDING_CURVE == hashlib.sha256(b"account:BondingCurve").digest()[:8]


def test_create_fee_sharing_config_passes_every_idl_account():
    """pump_fees idl: 13 accounts, the last 3 optional. Anchor wants an absent optional account passed as the program id
    in its slot; leaving them out fails on mainnet with AccountNotEnoughKeys on `pool` (seen in simulation 2026-10-28)."""
    idl_order = ["event_authority", "program", "payer", "global", "mint", "sharing_config", "system_program",
                 "bonding_curve", "pump_program", "pump_event_authority", "pool", "pump_amm_program",
                 "pump_amm_event_authority"]
    payer, mint = Pubkey.new_unique(), Pubkey.new_unique()
    ix = pf.create_fee_sharing_config_ix(payer, mint)
    assert len(ix.accounts) == len(idl_order)
    got = dict(zip(idl_order, ix.accounts))
    assert got["payer"].pubkey == payer and got["payer"].is_signer and got["payer"].is_writable
    assert got["sharing_config"].pubkey == pf.sharing_config_pda(mint) and got["sharing_config"].is_writable
    assert got["bonding_curve"].pubkey == pf.bonding_curve_pda(mint) and got["bonding_curve"].is_writable
    for name in ("pool", "pump_amm_program", "pump_amm_event_authority"):
        a = got[name]
        assert (a.pubkey, a.is_signer, a.is_writable) == (pf.PUMP_FEES_PROGRAM, False, False), name


def test_split_for_and_the_share_rules():
    assert pf.split_for(WALLET, LAUNCHER, 9000) == [Share(WALLET, 9000), Share(LAUNCHER, 1000)]
    assert pf.split_for(LAUNCHER, LAUNCHER, 9000) == [Share(LAUNCHER, 10_000)]  # VIGIL's own coin: one shareholder
    assert pf.split_for(WALLET, LAUNCHER, 10_000) == [Share(WALLET, 10_000)]
    for bad, words in [([Share(WALLET, 9000), Share(LAUNCHER, 999)], "add up to 10000"),
                       ([Share(WALLET, 5000), Share(WALLET, 5000)], "same address twice"),
                       ([Share(WALLET, 10_000), Share(LAUNCHER, 0)], "above 0"),
                       ([Share(str(Pubkey.new_unique()), 1000) for _ in range(10)] + [Share(WALLET, 0)], "1 to 10"),
                       ([], "1 to 10"), ([Share("nope", 10_000)], "Solana address")]:
        with pytest.raises(LaunchBlocked, match=words):
            pf.check_shares(bad)


def test_update_fee_shares_v2_encodes_requester_9000_then_launcher_1000(settings, db):
    node = _funded()
    node.set_account(pf.bonding_curve_pda(MINT), pf.PUMP_PROGRAM, curve_bytes(LAUNCHER_KP.pubkey()))
    lp = _launcher(settings, db, node, FakeWeb())
    tx, want = lp.fee_split_tx(MINT, pf.split_for(WALLET, LAUNCHER, 9000))
    msg = tx.message
    keys = list(msg.account_keys)
    assert keys[0] == LAUNCHER_KP.pubkey() and msg.header.num_required_signatures == 1  # the creator signs alone
    progs = [(keys[ix.program_id_index], bytes(ix.data)) for ix in msg.instructions]
    assert [p for p, _ in progs] == [pf.COMPUTE_BUDGET_PROGRAM, pf.COMPUTE_BUDGET_PROGRAM, pf.PUMP_PROGRAM,
                                     pf.PUMP_FEES_PROGRAM, pf.PUMP_FEES_PROGRAM]
    assert progs[2][1] == pf.DISC_SWEEP_CREATOR_FEE and progs[3][1] == pf.DISC_CREATE_FEE_SHARING_CONFIG
    data = progs[4][1]
    assert data[:8] == bytes([111, 251, 49, 6, 78, 78, 106, 18])
    n = struct.unpack_from("<I", data, 8)[0]
    shares = [(str(Pubkey.from_bytes(data[12 + 34 * i:44 + 34 * i])), struct.unpack_from("<H", data, 44 + 34 * i)[0])
              for i in range(n)]
    assert shares == [(WALLET, 9000), (LAUNCHER, 1000)] and sum(b for _, b in shares) == 10_000
    assert len(data) == 8 + 4 + 34 * 2
    ix = msg.instructions[4]
    acc = [keys[i] for i in bytes(ix.accounts)]
    assert len(acc) == 19 + 1 + 1  # the IDL's 19, the current shareholder (the launcher), the canonical pool
    assert acc[2] == LAUNCHER_KP.pubkey() and msg.is_signer(keys.index(acc[2]))
    assert acc[5] == pf.sharing_config_pda(MINT) and acc[6] == pf.bonding_curve_pda(MINT)
    assert acc[7] == pf.creator_vault_pda(pf.sharing_config_pda(MINT)) and acc[14] == pf.WSOL_MINT
    assert acc[17] == pf.amm_creator_vault_pda(pf.sharing_config_pda(MINT))
    assert acc[19] == LAUNCHER_KP.pubkey() and acc[20] == pf.canonical_pool_pda(MINT)
    create = [keys[i] for i in bytes(msg.instructions[3].accounts)]
    assert len(create) == 13 and create[2] == LAUNCHER_KP.pubkey() and create[5] == pf.sharing_config_pda(MINT)
    assert create[7] == pf.bonding_curve_pda(MINT) and create[8] == pf.PUMP_PROGRAM
    assert create[10:] == [pf.PUMP_FEES_PROGRAM] * 3  # the optional pool, amm program and amm event authority
    sweep = [keys[i] for i in bytes(msg.instructions[2].accounts)]
    assert len(sweep) == 13 and sweep[9] == pf.creator_vault_pda(LAUNCHER_KP.pubkey())  # the creator before the split


def test_pdas_use_the_documented_seeds():
    mint = Pubkey.from_string(MINT)
    assert pf.sharing_config_pda(mint) == Pubkey.find_program_address([b"sharing-config", bytes(mint)],
                                                                      pf.PUMP_FEES_PROGRAM)[0]
    assert pf.bonding_curve_pda(mint) == Pubkey.find_program_address([b"bonding-curve", bytes(mint)], pf.PUMP_PROGRAM)[0]
    assert pf.event_authority(pf.PUMP_FEES_PROGRAM) == \
        Pubkey.find_program_address([b"__event_authority"], pf.PUMP_FEES_PROGRAM)[0]


def test_the_guard_simulates_but_never_sends_an_unverified_split(settings, db):
    node = _funded()
    node.set_account(pf.bonding_curve_pda(MINT), pf.PUMP_PROGRAM, curve_bytes(LAUNCHER_KP.pubkey()))
    lp = _launcher(settings, db, node, FakeWeb())
    assert pf.FEE_PROGRAM_VERIFIED is False and lp.verified is False
    with pytest.raises(FeeSplitUnverified) as e:
        lp.set_fee_split(MINT, pf.split_for(WALLET, LAUNCHER, 9000))
    assert node.sent == [] and len(node.simulated) == 1
    ixs = e.value.simulation["instructions"]  # for review in the journal; no signed bytes kept
    assert "logs" in e.value.simulation and "tx_base64" not in e.value.simulation
    assert ixs[-1]["program"] == str(pf.PUMP_FEES_PROGRAM) and ixs[-1]["data_hex"].startswith("6ffb31064e4e6a12")
    assert ixs[-1]["accounts"][2] == {"pubkey": LAUNCHER, "signer": True, "writable": True}
    assert node.accounts[str(pf.bonding_curve_pda(MINT))][1] == curve_bytes(LAUNCHER_KP.pubkey())  # nothing changed
    # payouts sit behind the same guard (here a split set by hand, with fees waiting)
    cfg = pf.sharing_config_pda(MINT)
    node.set_account(pf.bonding_curve_pda(MINT), pf.PUMP_PROGRAM, curve_bytes(cfg))
    node.set_account(cfg, pf.PUMP_FEES_PROGRAM, config_bytes(Pubkey.from_string(MINT), LAUNCHER_KP.pubkey(),
                                                             [(REQUESTER, 9000), (LAUNCHER_KP.pubkey(), 1000)], True))
    node.balances[str(pf.creator_vault_pda(cfg))] = 10**8
    with pytest.raises(FeeSplitUnverified):
        lp.distribute_fees(MINT)
    assert node.sent == []


def test_a_verified_split_is_sent_once_read_back_and_then_paid_out(settings, db):
    node = _funded()
    node.set_account(pf.bonding_curve_pda(MINT), pf.PUMP_PROGRAM, curve_bytes(LAUNCHER_KP.pubkey()))
    lp = _launcher(settings, db, node, FakeWeb(), fee_split_verified=True)
    shares = pf.split_for(WALLET, LAUNCHER, 9000)
    sig = lp.set_fee_split(MINT, shares)
    assert sig == str(node.sent[0].signatures[0]) and len(node.sent) == 1
    cfg = lp.sharing_config(MINT)
    assert cfg["admin_revoked"] and [(s.address, s.bps) for s in cfg["shareholders"]] == [(WALLET, 9000), (LAUNCHER, 1000)]
    assert lp.set_fee_split(MINT, shares) is None and len(node.sent) == 1  # already locked as asked: nothing sent
    with pytest.raises(LaunchBlocked, match="different shares"):
        lp.set_fee_split(MINT, pf.split_for(WALLET, LAUNCHER, 8000))
    assert lp.distribute_fees(MINT) is None  # nothing waiting yet
    node.balances[str(pf.creator_vault_pda(pf.sharing_config_pda(MINT)))] = 50_000_000
    paid = lp.distribute_fees(MINT)
    assert paid and len(node.sent) == 2
    msg = node.sent[1].message
    keys = list(msg.account_keys)
    dist = msg.instructions[-1]
    assert keys[dist.program_id_index] == pf.PUMP_PROGRAM and bytes(dist.data) == pf.DISC_DISTRIBUTE_CREATOR_FEES_V2 + b"\0"
    acc = [keys[i] for i in bytes(dist.accounts)]
    assert len(acc) == 12 + 2 and acc[12:] == [REQUESTER, LAUNCHER_KP.pubkey()]  # sharing_config order
    assert journal_recent(db, kind="launchpad_fee_split") and journal_recent(db, kind="launchpad_distributed")


def test_a_graduated_coin_or_someone_elses_coin_is_not_split(settings, db):
    node = _funded()
    lp = _launcher(settings, db, node, FakeWeb(), fee_split_verified=True)
    node.set_account(pf.bonding_curve_pda(MINT), pf.PUMP_PROGRAM, curve_bytes(LAUNCHER_KP.pubkey(), complete=True))
    with pytest.raises(LaunchBlocked, match="graduated"):
        lp.set_fee_split(MINT, pf.split_for(WALLET, LAUNCHER, 9000))
    node.set_account(pf.bonding_curve_pda(MINT), pf.PUMP_PROGRAM, curve_bytes(REQUESTER))
    with pytest.raises(LaunchBlocked, match="isn't this coin's creator"):
        lp.set_fee_split(MINT, pf.split_for(WALLET, LAUNCHER, 9000))
    assert node.sent == []


# --- the whole loop with the real launcher -----------------------------------------------------------------------------

def test_end_to_end_launch_with_the_guard_on_then_off(settings, db):
    node, web = _funded(), FakeWeb()
    ctx = _ctx(settings, db, rpc=node.rpc, launcher_wallet_private_key=LAUNCHER_SECRET, pinata_jwt="test-jwt",
               launch_dev_buy_sol=0.01)
    take_request(ctx, _m("70"))
    lp = PumpLauncher(ctx.settings, node.rpc, db, transport=web.transport)
    launchpad_once(ctx, lp)
    _announced(db, "990")
    assert launchpad_once(ctx, lp)["launched"] == 1
    r = rows(db, "SELECT * FROM launch_requests")[0]
    assert r["status"] == "launched" and r["token"] == web.portal_requests[0]["mint"]
    assert r["tx_hash"] == str(node.sent[0].signatures[0]) and r["cost_lamports"] == str(CREATE_COST)
    assert r["metadata_uri"].startswith("https://ipfs.io/ipfs/bafy") and r["recipient"] == WALLET
    meta = web.pins[-1]["json"]
    assert meta["twitter"] == "https://x.com/catcoin" and "launched via @VigilBot" in meta["description"]
    assert r["fee_shares"] is None and len(node.sent) == 1  # guard on: the split waits, nothing irreversible sent
    assert journal_recent(db, kind="launchpad_fee_split_unverified")
    kv_set(db, f"launch_split_next:{r['id']}", 0)
    lp.fee_split_verified = True  # what flipping FEE_PROGRAM_VERIFIED does after the mainnet check
    assert launchpad_once(ctx, lp)["fee_splits"] == 1
    r = rows(db, "SELECT * FROM launch_requests")[0]
    assert json.loads(r["fee_shares"]) == [{"address": WALLET, "bps": 9000}, {"address": LAUNCHER, "bps": 1000}]
    assert r["fee_share_tx"] == str(node.sent[1].signatures[0])
    upd = node.sent[1].message.instructions[-1]
    assert base64.b64encode(bytes(upd.data)[:8]) == base64.b64encode(pf.DISC_UPDATE_FEE_SHARES_V2)


# --- ctagent launch-test -----------------------------------------------------------------------------------------------

def _launch_test(settings, db, node, tmp_path, answer="TST", **kw):
    from ct_agent import cli
    img = tmp_path / "coin.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    s = settings.model_copy(update={"launcher_wallet_private_key": LAUNCHER_SECRET, "pinata_jwt": "test-jwt"})
    asked = []
    args = {"rpc": node.rpc, "conn": db, "transport": FakeWeb().transport, "ask": lambda q: asked.append(q) or answer}
    code = cli._launch_test(s, str(img), "Test Coin", kw.pop("symbol", "tst"), kw.pop("fees_to", WALLET), **args, **kw)
    return code, asked


def test_launch_test_launches_then_locks_the_split_and_reads_it_back(settings, db, tmp_path, capsys):
    node = _funded()
    code, asked = _launch_test(settings, db, node, tmp_path)
    out = capsys.readouterr().out
    assert code == 0 and "type TST to go ahead" in asked[0]  # the ticker, upper-cased, is the confirmation
    assert len(node.sent) == 2  # the launch, then the split
    assert "on chain: locked, 90% to " + WALLET + ", 10% to " + LAUNCHER in out
    assert "set CT_LAUNCH_FEE_SPLIT_VERIFIED=true" in out


def test_launch_test_sends_nothing_without_the_ticker_typed(settings, db, tmp_path, capsys):
    node = _funded()
    code, _ = _launch_test(settings, db, node, tmp_path, answer="yes")
    assert code == 1 and node.sent == [] and "nothing sent" in capsys.readouterr().out


def test_launch_test_refuses_a_big_dev_buy_or_the_launcher_as_fee_wallet(settings, db, tmp_path, capsys):
    node = _funded()
    assert _launch_test(settings, db, node, tmp_path, dev_buy=0.5)[0] == 1
    assert _launch_test(settings, db, node, tmp_path, fees_to=LAUNCHER)[0] == 1
    assert _launch_test(settings, db, node, tmp_path, fees_to="not-a-wallet")[0] == 1
    assert node.sent == []
    out = capsys.readouterr().out
    assert "capped at 0.05 SOL" in out and "another wallet than the launcher" in out and "isn't a Solana address" in out
