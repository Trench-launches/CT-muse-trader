"""The launchpad dashboard's data: launched coins with curve progress and price, payouts, and stats."""
import base64
import json
import struct

from fastapi.testclient import TestClient

from ct_agent.api import site
from ct_agent.api.app import create_app
from ct_agent.db import journal_append, utcnow
from ct_agent.launchpad import pumpfun as pf

MINT_A, MINT_B = "So1aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "So1bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
REQ, VIGIL = "Req1111111111111111111111111111111111111111", "Vig1111111111111111111111111111111111111111"


def curve(real_tokens: int, real_sol: int = 0, complete: bool = False) -> bytes:
    return pf.DISC_BONDING_CURVE + struct.pack("<QQQQQ", 0, 0, real_tokens, real_sol, 10**15) + bytes([complete]) + bytes(32)


def _launched(db, n, mint, symbol, when=None):
    shares = [{"address": REQ, "bps": 9000}, {"address": VIGIL, "bps": 1000}]
    db.execute("INSERT INTO launch_requests(x_post_id,author_id,handle,created_at,status,name,symbol,token,recipient,"
               "fee_shares,launched_at,tx_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
               (f"post{n}", "u1", "catfan", when or utcnow(), "launched", f"{symbol} Coin", symbol, mint, REQ,
                json.dumps(shares), when or utcnow(), f"SIG{n}"))
    db.commit()


def test_curve_progress_from_the_account_bytes():
    assert site.curve_progress(curve(site.CURVE_START_TOKENS)) == {"progress_pct": 0.0, "graduated": False, "curve_sol": 0.0}
    half = site.curve_progress(curve(site.CURVE_START_TOKENS // 2, 42 * 10**9))
    assert half == {"progress_pct": 50.0, "graduated": False, "curve_sol": 42.0}
    assert site.curve_progress(curve(0, 85 * 10**9, complete=True))["graduated"] is True
    assert site.curve_progress(None) is None and site.curve_progress(b"short") is None
    assert site.curve_progress(curve(965_000_000 * 10**6)) is None  # a curve that starts bigger: no wrong 0%


def test_board_lists_coins_with_progress_price_and_the_requesters_payouts(settings, db):
    _launched(db, 1, MINT_A, "CAT")
    _launched(db, 2, MINT_B, "DOG", when="2026-01-01T00:00:00+00:00")
    journal_append(db, "launchpad_distributed", {"mint": MINT_A, "signature": "PAY1", "lamports": 10**8,
                                                  "shares": [{"address": REQ, "bps": 9000}]}, ref=MINT_A, mode="live")
    db.execute("INSERT INTO launch_requests(x_post_id,author_id,created_at,status,reason) VALUES('p9','u2',?,'refused','x')",
               (utcnow(),))
    b = site.launch_board(db, settings, curves=lambda ms: {MINT_A: curve(site.CURVE_START_TOKENS // 4)},
                          prices_usd=lambda ms: {MINT_A: 0.00002})
    cat = next(c for c in b["coins"] if c["symbol"] == "CAT")
    assert cat["progress_pct"] == 75.0 and cat["graduated"] is False and cat["market_cap_usd"] == 20000.0
    assert cat["creator_share_bps"] == 9000 and cat["fee_split_locked"] is True
    dog = next(c for c in b["coins"] if c["symbol"] == "DOG")
    assert dog["progress_pct"] is None and dog["price_usd"] is None  # no chain or market data: left empty
    assert "recipient" not in cat  # the board never needs the requester's wallet key-by-key; shares carry it
    assert b["payouts"][0] == {"at": b["payouts"][0]["at"], "token": MINT_A, "symbol": "CAT", "signature": "PAY1",
                               "total_sol": 0.1, "creator_sol": 0.09}
    st = b["stats"]
    assert (st["launched"], st["launched_24h"], st["requests_24h"], st["refused_24h"]) == (2, 1, 2, 1)
    assert st["creator_fees_paid_sol"] == 0.09 and st["creator_share_bps"] == 9000


def test_board_survives_failing_chain_and_market_reads(settings, db):
    _launched(db, 1, MINT_A, "CAT")

    def boom(_):
        raise RuntimeError("rpc down")
    b = site.launch_board(db, settings, curves=boom, prices_usd=boom)
    assert b["coins"][0]["symbol"] == "CAT" and b["coins"][0]["progress_pct"] is None


def test_board_endpoint_reads_every_curve_in_one_call(settings, db):
    _launched(db, 1, MINT_A, "CAT")
    calls = []

    class Rpc:
        def multiple_accounts(self, pubkeys, encoding="jsonParsed"):
            calls.append((pubkeys, encoding))
            return [{"data": [base64.b64encode(curve(site.CURVE_START_TOKENS // 2)).decode(), "base64"]}]

    class Market:
        def prices_usd(self, mints):
            return {MINT_A: 0.00001}

    site._BOARD.clear()
    app = create_app(settings)
    app.state.chat_services = {"rpc": Rpc(), "market": Market()}
    d = TestClient(app).get("/launchpad/board").json()
    assert d["coins"][0]["progress_pct"] == 50.0 and d["coins"][0]["market_cap_usd"] == 10000.0
    assert calls == [([str(pf.bonding_curve_pda(MINT_A))], "base64")]
    site._BOARD.clear()


def test_coin_images_come_through_pinatas_gateway(settings, db):
    """ipfs.io rate-limits browsers (429); the same pinned file is served through Pinata's gateway."""
    assert site.shown_image("https://ipfs.io/ipfs/bafyimg") == "https://gateway.pinata.cloud/ipfs/bafyimg"
    assert site.shown_image("https://pbs.twimg.com/media/x.jpg") == "https://pbs.twimg.com/media/x.jpg"
    assert site.shown_image(None) is None
    _launched(db, 1, MINT_A, "CAT")
    db.execute("UPDATE launch_requests SET image_uri='https://ipfs.io/ipfs/bafycat'")
    assert site.launch_board(db, settings)["coins"][0]["logo"] == "https://gateway.pinata.cloud/ipfs/bafycat"
    assert site.recent_launches(db)[0]["logo"] == "https://gateway.pinata.cloud/ipfs/bafycat"
