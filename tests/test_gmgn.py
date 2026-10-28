import base64
import functools
import json
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest
from Crypto.PublicKey import ECC
from Crypto.Signature import eddsa

import base58
from solders.keypair import Keypair

from ct_agent.chain.keys import SOL_MINT
from ct_agent.config import Settings
from ct_agent.db import journal_recent
from ct_agent.execution.gmgn import NATIVE, GmgnClient, _qs
from ct_agent.execution.live import LiveExecutor, LiveTradingBlocked
from ct_agent.execution.risk import RiskManager

from .fakes import mint

SOL = 10**9
WALLET_KEY = Keypair()
KEY = ECC.generate(curve="ed25519")
PEM = KEY.export_key(format="PEM")


def _gmgn(orders: list[dict], seen: list[httpx.Request]) -> httpx.MockTransport:
    """GMGN's API: checks every signature like the real one, then answers swaps and order polls."""
    verifier = eddsa.new(KEY.public_key(), "rfc8032")

    def h(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        q = dict(parse_qsl(urlsplit(str(req.url)).query))
        msg = f"{req.url.path}:{_qs(q)}:{req.content.decode()}:{q['timestamp']}".encode()
        try:
            verifier.verify(msg, base64.b64decode(req.headers["X-Signature"]))
        except (ValueError, KeyError):
            return httpx.Response(401, json={"code": 401, "error": "AUTH_SIGNATURE_INVALID"})
        if req.url.path == "/v1/trade/swap":
            return httpx.Response(200, json={"code": 0, "data": {"order_id": "o1", "hash": "5sig", "status": "pending"}})
        return httpx.Response(200, json={"code": 0, "data": orders.pop(0)})
    return httpx.MockTransport(h)


def _executor(db, tmp_path, orders, seen, **kw):
    s = Settings(_env_file=None, trading_mode="live", live_execution_reviewed=True, live_venue="gmgn",
                 trading_wallet_private_key=base58.b58encode(bytes(WALLET_KEY)).decode(),
                 kill_switch_file=str(tmp_path / "KILL"), gmgn_api_key="k", gmgn_private_key=PEM, **kw)
    from ct_agent.chain.keys import MAINNET_GENESIS
    rpc = SimpleNamespace(get_balance=lambda w: SOL, genesis_hash=lambda: MAINNET_GENESIS,
                          token_balance=lambda owner, m: 0)
    ex = LiveExecutor(s, rpc, SimpleNamespace(), db, RiskManager(s, db))
    gm = GmgnClient("k", PEM, transport=_gmgn(orders, seen))
    gm.wait = functools.partial(GmgnClient.wait, gm, timeout_s=5, every_s=0)  # no real sleeping between polls
    ex._gmgn = gm
    return ex


def test_gmgn_buy_is_signed_polled_and_filled(db, tmp_path):
    seen: list[httpx.Request] = []
    report = {"input_amount": str(SOL // 100), "output_amount": str(5000 * 10**6)}
    ex = _executor(db, tmp_path, [{"status": "processed"}, {"status": "confirmed", "hash": "5sig", "report": report}], seen)
    fill = ex.swap(mint(1), "buy", SOL // 100)
    assert (fill.quote_amount, fill.token_amount, fill.tx_hash) == (SOL // 100, 5000 * 10**6, "5sig")
    body = json.loads(seen[0].content)
    assert body["chain"] == "sol" and body["from_address"] == ex.wallet == str(WALLET_KEY.pubkey())
    assert body["auto_slippage"] is True and NATIVE == SOL_MINT
    assert body["input_token"] == SOL_MINT and body["output_token"] == mint(1) and body["input_amount"] == str(SOL // 100)
    assert journal_recent(db, kind="live_fill")[0]["payload"]["venue"] == "gmgn"


def test_gmgn_failed_order_is_not_a_fill(db, tmp_path):
    ex = _executor(db, tmp_path, [{"status": "failed", "error_status": "slippage"}], [], gmgn_slippage_pct=5)
    with pytest.raises(LiveTradingBlocked, match="failed: slippage"):
        ex.swap(mint(1), "buy", SOL // 100)
    assert journal_recent(db, kind="live_failed")[0]["payload"]["stage"] == "mined"
    assert not journal_recent(db, kind="live_fill")


def test_gmgn_needs_its_keys(db, tmp_path):
    ex = _executor(db, tmp_path, [], [])
    ex.s = ex.s.model_copy(update={"gmgn_private_key": ""})
    with pytest.raises(LiveTradingBlocked, match="signing key is missing"):
        ex.swap(mint(1), "buy", SOL // 100)
