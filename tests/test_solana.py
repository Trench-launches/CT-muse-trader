"""The Solana foundations: SOL units, base58 keys (never lowercased), mint safety, holders, Jupiter signing."""
import base64
import json

import base58
import httpx
import pytest
from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from ct_agent import units
from ct_agent.chain import keys
from ct_agent.chain import token as tok
from ct_agent.chain.rpc import redact
from ct_agent.chain.solana import SolanaRpc
from ct_agent.execution.jupiter import Jupiter, JupiterError, sign

from .fakes import Coin, FakeJupiter, FakeSolana, mint, wallet

BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"


def test_sol_units_and_token_decimals():
    assert units.QUOTE_SYMBOL == "SOL" and units.LAMPORTS == 10**9
    assert units.lamports(0.01) == 10_000_000 and units.sol(1_500_000_000) == 1.5
    assert units.to_base(1.5, 6) == 1_500_000 and units.from_base("2500000", 6) == 2.5
    assert units.to_base(0.0000019, 6) == 1  # rounds down: never spend more than held


def test_base58_keys_round_trip_and_are_never_lowercased():
    kp = Keypair()
    secret = base58.b58encode(bytes(kp)).decode()
    assert keys.load_keypair(secret).pubkey() == kp.pubkey()
    assert keys.load_keypair(json.dumps(list(bytes(kp)))).pubkey() == kp.pubkey()  # solana-keygen file format
    with pytest.raises(ValueError):
        keys.load_keypair("0x" + "11" * 32)
    assert keys.is_pubkey(BONK) and keys.is_pubkey(keys.SOL_MINT) and keys.is_pubkey(keys.TOKEN_2022_PROGRAM)
    assert not keys.is_pubkey(BONK.lower()) and not keys.is_pubkey("0x" + "ab" * 20) and not keys.is_pubkey(BONK + "1")
    assert keys.find_pubkeys(f"ca: {BONK}, again {BONK}; evm 0x{'ab' * 20}") == [BONK]
    assert keys.short(BONK) == "DezX…B263"


def test_rpc_urls_never_leak_their_key():
    assert redact("https://mainnet.helius-rpc.com/?api-key=secret123") == "https://mainnet.helius-rpc.com/?api-key=***"
    assert "secret" not in redact("https://solana-mainnet.g.alchemy.com/v2/secret")


def _rpc(*coins, das=True) -> tuple[SolanaRpc, FakeSolana]:
    ch = FakeSolana(list(coins))
    ch.das = das
    return SolanaRpc("https://rpc.test", transport=ch.transport(), retries=0), ch


def test_mint_safety_reads_authorities_and_token_2022_extensions():
    clean = Coin(mint(1))
    mintable = Coin(mint(2), mint_authority=wallet(1))
    freezable = Coin(mint(3), freeze_authority=wallet(1))
    t22 = keys.TOKEN_2022_PROGRAM
    delegate = Coin(mint(4), program=t22, extensions=[{"extension": "permanentDelegate", "state": {"delegate": wallet(2)}}])
    hooked = Coin(mint(5), program=t22, extensions=[{"extension": "transferHook", "state": {"programId": wallet(3)}}])
    unhooked = Coin(mint(6), program=t22, extensions=[{"extension": "transferHook", "state": {"programId": None}},
                                                      {"extension": "metadataPointer", "state": {}}])
    taxed = Coin(mint(7), program=t22, extensions=[{"extension": "transferFeeConfig", "state": {
        "olderTransferFee": {"transferFeeBasisPoints": 0}, "newerTransferFee": {"transferFeeBasisPoints": 250}}}])
    rpc, _ = _rpc(clean, mintable, freezable, delegate, hooked, unhooked, taxed)
    b = {c.mint: tok.safety_blockers(tok.mint_info(rpc, c.mint)) for c in (clean, mintable, freezable, delegate,
                                                                         hooked, unhooked, taxed)}
    assert b[clean.mint] == [] and b[unhooked.mint] == []
    assert b[mintable.mint] == ["its creator can still mint more coins"]
    assert b[freezable.mint] == ["its creator can freeze holders' coins"]
    assert b[delegate.mint] == ["a permanent delegate can move anyone's coins"]
    assert b[hooked.mint] == ["every transfer runs someone's program"]
    assert b[taxed.mint] == ["a 2.5% tax on every transfer"]
    assert tok.safety_blockers(tok.mint_info(rpc, taxed.mint), max_transfer_fee_bps=300) == []
    info = tok.mint_info(rpc, clean.mint)
    assert (info.decimals, info.supply, info.is_token_2022) == (6, clean.supply, False)
    assert not tok.mint_info(rpc, wallet(9)).exists and tok.safety_blockers(tok.mint_info(rpc, "0xnope")) == \
        ["not a Solana token mint"]
    assert tok.verify(rpc, clean.mint) == (True, "token mint") and tok.verify(rpc, wallet(9))[0] is False


def test_holders_count_wallets_only():
    supply = 10**15
    c = Coin(mint(1), supply=supply, holders=[(wallet(99), supply // 2, True),   # the pool: a program, not a holder
                                              (keys.INCINERATOR, supply // 10, False),  # burned
                                              (wallet(1), supply // 20, False), (wallet(1), supply // 100, False),
                                              (wallet(2), supply // 50, False)])
    rpc, _ = _rpc(c)
    h = tok.holder_facts(rpc, c.mint, supply=supply, creator=wallet(2))
    assert h["top_holder_share"] == 0.06  # wallet 1's two accounts add up; the pool and the burn don't count
    assert h["top10_share"] == 0.08 and h["wallets_counted"] == 2 and h["creator_share"] == 0.02
    assert tok.holder_facts(rpc, c.mint, supply=0) == {"error": "no supply"}


def test_token_meta_comes_from_das_and_is_empty_without_it():
    c = Coin(mint(1), name="Hot", symbol="HOT", description="a cat")
    rpc, _ = _rpc(c)
    assert tok.token_meta(rpc, c.mint) == {"name": "Hot", "symbol": "HOT", "image": c.image, "description": "a cat"}
    rpc, _ = _rpc(c, das=False)
    assert tok.token_meta(rpc, c.mint) == {}


def test_jupiter_sign_round_trip():
    kp = Keypair()
    msg = MessageV0.try_compile(kp.pubkey(), [], [], Hash.default())
    unsigned = base64.b64encode(bytes(VersionedTransaction.populate(msg, [Signature.default()]))).decode()
    signed = VersionedTransaction.from_bytes(base64.b64decode(sign(unsigned, kp)))
    assert signed.message == msg and signed.signatures[0] != Signature.default() and all(signed.verify_with_results())
    with pytest.raises(Exception):
        sign(unsigned, Keypair())  # not the transaction's signer


def test_jupiter_client_against_the_fake():
    ch = FakeSolana([Coin(mint(1), price_usd=0.0001), Coin(mint(2), routable=False)])
    jup = Jupiter("https://api.jup.ag", "k", transport=FakeJupiter(ch).transport(), min_interval_s=0)
    q = jup.order(keys.SOL_MINT, mint(1), units.lamports(0.01))
    assert int(q["outAmount"]) > 0 and q["transaction"] is None  # no taker: just a quote
    with pytest.raises(JupiterError, match="no route"):
        jup.order(keys.SOL_MINT, mint(2), 1000)
    px = jup.prices([mint(1), mint(2), keys.SOL_MINT])
    assert px[mint(1)]["usdPrice"] == 0.0001 and mint(2) not in px and px[keys.SOL_MINT]["usdPrice"] == 150.0


def test_jupiter_pauses_and_retries_a_rate_limit():
    hits = []

    def h(req):
        hits.append(1)
        if len(hits) == 1:
            return httpx.Response(429, headers={"retry-after": "0"}, json={})
        return httpx.Response(200, json={keys.SOL_MINT: {"usdPrice": 150.0}})
    jup = Jupiter("https://api.jup.ag", "k", transport=httpx.MockTransport(h), min_interval_s=0)
    assert jup.prices([keys.SOL_MINT])[keys.SOL_MINT]["usdPrice"] == 150.0 and len(hits) == 2
