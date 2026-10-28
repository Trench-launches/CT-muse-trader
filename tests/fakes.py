"""Offline fakes that speak the real wire formats.

FakeSolana answers the JSON-RPC methods SolanaRpc uses (mints, token accounts, holders, balances, send/confirm),
FakeJupiter answers Swap V2 order/execute (real unsigned VersionedTransactions, signatures checked on execute)
and Price API v3, and `market_transport` answers DexScreener, CoinGecko and the scanners' Supabase. They all read
the same coins, so moving a coin's price moves every feed at once.
"""
from __future__ import annotations

import base64
import hashlib
import itertools
import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

import base58
import httpx
from solders.hash import Hash
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from ct_agent.chain.keys import MAINNET_GENESIS, PUMP_AMM_PROGRAM, SOL_MINT, SYSTEM_PROGRAM, TOKEN_PROGRAM

SOL_USD = 150.0


def _b58(tag: str) -> str:
    return base58.b58encode(hashlib.sha256(tag.encode()).digest()).decode()


def mint(n: int) -> str:
    """A deterministic, valid, mixed-case base58 address (so any lowercasing breaks the tests)."""
    return _b58(f"mint{n}")


def wallet(n: int) -> str:
    return _b58(f"wallet{n}")


@dataclass
class Coin:
    mint: str
    symbol: str = "TST"
    name: str = "Test"
    decimals: int = 6
    supply: int = 1_000_000_000 * 10**6
    price_usd: float = 0.0001
    liquidity_usd: float = 60_000.0
    volume_h1_usd: float = 20_000.0
    volume_h24_usd: float = 200_000.0
    dex: str = "pumpswap"
    mint_authority: str | None = None
    freeze_authority: str | None = None
    program: str = TOKEN_PROGRAM
    extensions: list[dict[str, Any]] = field(default_factory=list)
    # (owner, base units, owned by a program (curve/pool) rather than a wallet)
    holders: list[tuple[str, int, bool]] = field(default_factory=list)
    description: str = ""
    twitter: str = ""
    website: str = ""
    image: str = "https://ipfs.io/ipfs/QmLogo"
    routable: bool = True
    impact_pct: float = 0.1  # Jupiter's priceImpact for any swap, in percent

    @property
    def pool(self) -> str:
        return _b58(f"pool{self.mint}")

    def pair(self) -> dict[str, Any]:
        return {"chainId": "solana", "dexId": self.dex, "pairAddress": self.pool,
                "baseToken": {"address": self.mint, "name": self.name, "symbol": self.symbol},
                "quoteToken": {"address": SOL_MINT, "symbol": "SOL"},
                "priceUsd": str(self.price_usd), "priceNative": str(self.price_usd / SOL_USD),
                "volume": {"h1": self.volume_h1_usd, "h24": self.volume_h24_usd},
                "liquidity": {"usd": self.liquidity_usd}, "txns": {"h1": {"buys": 40, "sells": 30}},
                "marketCap": self.price_usd * self.supply / 10 ** self.decimals,
                "info": {"imageUrl": "https://dd.dexscreener.com/img.png",
                         "websites": [{"url": self.website}] if self.website else [],
                         "socials": [{"type": "twitter", "url": self.twitter}] if self.twitter else []}}


class FakeSolana:
    """A Solana node over an httpx.MockTransport."""

    def __init__(self, coins: list[Coin] | None = None):
        self.coins: dict[str, Coin] = {c.mint: c for c in coins or []}
        self.lamports: dict[str, int] = {}
        self.balances: dict[tuple[str, str], int] = {}      # (owner, mint) -> base units
        self.program_owned: set[str] = set()
        self.statuses: dict[str, dict[str, Any]] = {}
        self.genesis = MAINNET_GENESIS
        self.das = True
        self.calls: list[str] = []

    def add(self, coin: Coin) -> Coin:
        self.coins[coin.mint] = coin
        return coin

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    # --- accounts -------------------------------------------------------------

    def _token_accounts(self, c: Coin) -> list[tuple[str, str, int]]:
        """(token account, owner, amount) for each holder."""
        return [(_b58(f"ta{c.mint}{i}"), owner, amt) for i, (owner, amt, _) in enumerate(c.holders)]

    def _account(self, pk: str, encoding: str) -> dict[str, Any] | None:
        c = self.coins.get(pk)
        if c:
            return {"owner": c.program, "lamports": 1_461_600, "executable": False,
                    "data": {"program": "spl-token", "parsed": {"type": "mint", "info": {
                        "decimals": c.decimals, "supply": str(c.supply), "mintAuthority": c.mint_authority,
                        "freezeAuthority": c.freeze_authority, "isInitialized": True,
                        **({"extensions": c.extensions} if c.extensions else {})}}}}
        for c in self.coins.values():
            for i, (owner, amt, prog) in enumerate(c.holders):
                if _b58(f"ta{c.mint}{i}") == pk:
                    return {"owner": c.program, "lamports": 2_039_280, "executable": False,
                            "data": {"program": "spl-token", "parsed": {"type": "account", "info": {
                                "owner": owner, "mint": c.mint, "tokenAmount": {
                                    "amount": str(amt), "decimals": c.decimals}}}}}
                if owner == pk:
                    return {"owner": PUMP_AMM_PROGRAM if prog else SYSTEM_PROGRAM, "lamports": 1, "executable": False,
                            "data": ["", "base64"]}
        if pk in self.program_owned:
            return {"owner": PUMP_AMM_PROGRAM, "lamports": 1, "executable": False, "data": ["", "base64"]}
        if pk in self.lamports:
            return {"owner": SYSTEM_PROGRAM, "lamports": self.lamports[pk], "executable": False, "data": ["", "base64"]}
        return None

    # --- rpc ------------------------------------------------------------------

    def _handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if isinstance(body, list):
            return httpx.Response(200, json=[self._one(b) for b in body])
        return httpx.Response(200, json=self._one(body))

    def _one(self, body: dict[str, Any]) -> dict[str, Any]:
        try:
            return {"jsonrpc": "2.0", "id": body["id"], "result": self._dispatch(body["method"], body["params"])}
        except _RpcFail as exc:
            return {"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32601, "message": str(exc)}}

    def _dispatch(self, method: str, params: Any) -> Any:
        self.calls.append(method)
        ctx = {"context": {"slot": 123}}
        if method == "getGenesisHash":
            return self.genesis
        if method == "getSlot":
            return 123
        if method == "getBalance":
            return {**ctx, "value": self.lamports.get(params[0], 0)}
        if method == "getAccountInfo":
            return {**ctx, "value": self._account(params[0], params[1].get("encoding", "base64"))}
        if method == "getMultipleAccounts":
            return {**ctx, "value": [self._account(p, params[1].get("encoding", "base64")) for p in params[0]]}
        if method == "getTokenSupply":
            c = self.coins.get(params[0])
            if not c:
                raise _RpcFail("Invalid param: not a Token mint")
            return {**ctx, "value": {"amount": str(c.supply), "decimals": c.decimals,
                                     "uiAmount": c.supply / 10 ** c.decimals}}
        if method == "getTokenLargestAccounts":
            c = self.coins.get(params[0])
            if not c:
                raise _RpcFail("Invalid param: not a Token mint")
            accts = sorted(self._token_accounts(c), key=lambda a: -a[2])[:20]
            return {**ctx, "value": [{"address": a, "amount": str(amt), "decimals": c.decimals,
                                      "uiAmount": amt / 10 ** c.decimals} for a, _, amt in accts]}
        if method == "getTokenAccountsByOwner":
            owner, m = params[0], params[1]["mint"]
            amt = self.balances.get((owner, m))
            if amt is None:
                return {**ctx, "value": []}
            dec = self.coins[m].decimals if m in self.coins else 6
            return {**ctx, "value": [{"pubkey": _b58(f"ata{owner}{m}"), "account": {"data": {"parsed": {"info": {
                "owner": owner, "mint": m, "tokenAmount": {"amount": str(amt), "decimals": dec}}}}}}]}
        if method == "getSignatureStatuses":
            return {**ctx, "value": [self.statuses.get(sig) for sig in params[0]]}
        if method == "getAsset":
            c = self.coins.get(params["id"])
            if not self.das or not c:
                raise _RpcFail("Method not found")
            return {"id": c.mint, "content": {"json_uri": "", "links": {"image": c.image},
                                              "metadata": {"name": c.name, "symbol": c.symbol,
                                                           "description": c.description}}}
        raise _RpcFail(f"unsupported {method}")


class _RpcFail(Exception):
    pass


class FakeJupiter:
    """Jupiter Swap V2 (order/execute) and Price API v3, priced from FakeSolana's coins."""

    def __init__(self, chain: FakeSolana, sol_usd: float = SOL_USD):
        self.chain = chain
        self.sol_usd = sol_usd
        self.orders: dict[str, dict[str, Any]] = {}
        self.calls: list[str] = []
        self.execute_error: str | None = None   # e.g. "slippage": the swap fails
        self.prices_down = False
        self._ids = itertools.count(1)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req.url.path)
        q = {k: v[0] for k, v in parse_qs(urlsplit(str(req.url)).query).items()}
        if req.url.path == "/swap/v2/order":
            return httpx.Response(200, json=self._order(q))
        if req.url.path == "/swap/v2/execute":
            return httpx.Response(200, json=self._execute(json.loads(req.content)))
        if req.url.path == "/price/v3":
            if self.prices_down:
                return httpx.Response(503, json={"error": "down"})
            out = {}
            for m in q["ids"].split(","):
                if m == SOL_MINT:
                    out[m] = {"usdPrice": self.sol_usd, "decimals": 9}
                elif m in self.chain.coins and self.chain.coins[m].routable:
                    c = self.chain.coins[m]
                    out[m] = {"usdPrice": c.price_usd, "decimals": c.decimals, "liquidity": c.liquidity_usd}
            return httpx.Response(200, json=out)
        return httpx.Response(404, json={"error": "not found"})

    def _order(self, q: dict[str, str]) -> dict[str, Any]:
        inp, out, amount = q["inputMint"], q["outputMint"], int(q["amount"])
        buy = inp == SOL_MINT
        c = self.chain.coins.get(out if buy else inp)
        if c is None or not c.routable:
            return {"errorCode": 1, "errorMessage": "No routes found", "inAmount": str(amount), "outAmount": ""}
        price_sol = c.price_usd / self.sol_usd  # SOL per whole token
        keep = 1 - c.impact_pct / 100
        if buy:
            got = int(amount / 1e9 / price_sol * keep * 10 ** c.decimals)
        else:
            got = int(amount / 10 ** c.decimals * price_sol * keep * 1e9)
        rid = f"r{next(self._ids)}"
        order = {"requestId": rid, "inputMint": inp, "outputMint": out, "inAmount": str(amount), "outAmount": str(got),
                 "priceImpact": -c.impact_pct, "priceImpactPct": str(-c.impact_pct / 100), "router": "iris",
                 "slippageBps": int(q.get("slippageBps", 50)), "swapType": "aggregator", "transaction": None}
        taker = q.get("taker")
        if taker:
            msg = MessageV0.try_compile(Pubkey.from_string(taker), [], [], Hash.default())
            tx = VersionedTransaction.populate(msg, [Signature.default()])
            order["transaction"] = base64.b64encode(bytes(tx)).decode()
            self.orders[rid] = {**order, "taker": taker}
        return order

    def _execute(self, body: dict[str, Any]) -> dict[str, Any]:
        o = self.orders.get(body["requestId"])
        tx = VersionedTransaction.from_bytes(base64.b64decode(body["signedTransaction"]))
        if o is None or not all(tx.verify_with_results()):
            return {"status": "Failed", "code": -2, "error": "invalid signature"}
        sig = str(tx.signatures[0])
        if self.execute_error:
            return {"status": "Failed", "code": -1, "error": self.execute_error, "signature": sig}
        taker, a, b = o["taker"], int(o["inAmount"]), int(o["outAmount"])
        ch = self.chain
        if o["inputMint"] == SOL_MINT:
            ch.lamports[taker] = ch.lamports.get(taker, 0) - a
            ch.balances[(taker, o["outputMint"])] = ch.balances.get((taker, o["outputMint"]), 0) + b
        else:
            ch.balances[(taker, o["inputMint"])] = ch.balances.get((taker, o["inputMint"]), 0) - a
            ch.lamports[taker] = ch.lamports.get(taker, 0) + b
        ch.statuses[sig] = {"slot": 124, "err": None, "confirmationStatus": "confirmed"}
        return {"status": "Success", "code": 0, "signature": sig, "slot": "124",
                "totalInputAmount": str(a), "totalOutputAmount": str(b),
                "inputAmountResult": str(a), "outputAmountResult": str(b)}


def market_transport(chain: FakeSolana, *, supabase: list[dict[str, Any]] | None = None,
                     fibbot: list[dict[str, Any]] | None = None, sol_usd: float = SOL_USD,
                     seen: list[str] | None = None) -> httpx.MockTransport:
    """DexScreener, GeckoTerminal (nothing), CoinGecko and the scanners' Supabase tables."""
    def h(req: httpx.Request) -> httpx.Response:
        url, host, path = str(req.url), req.url.host, req.url.path
        if seen is not None:
            seen.append(url)
        if "dexscreener" in host:
            if path.startswith("/token-pairs/v1/solana/"):
                c = chain.coins.get(path.rsplit("/", 1)[-1])
                return httpx.Response(200, json=[c.pair()] if c and c.dex else [])
            if path.startswith("/tokens/v1/solana/"):
                ms = path.rsplit("/", 1)[-1].split(",")
                return httpx.Response(200, json=[chain.coins[m].pair() for m in ms
                                                 if m in chain.coins and chain.coins[m].dex])
            if path == "/latest/dex/search":
                q = req.url.params.get("q", "").upper()
                return httpx.Response(200, json={"pairs": [c.pair() for c in chain.coins.values()
                                                           if c.symbol.upper() == q and c.dex]})
        if "geckoterminal" in host:
            return httpx.Response(404, json={})
        if "coingecko" in host:
            return httpx.Response(200, json={"solana": {"usd": sol_usd}})
        if "supabase" in host:
            if path.endswith("/alert_performance"):
                if "chain=eq.solana" not in url:
                    return httpx.Response(400, json={"message": "the test scanner only serves solana"})
                rows_ = list(supabase or [])
                if "select=id&" in url:
                    return httpx.Response(200, json=[{"id": max((r["id"] for r in rows_), default=0)}])
                return httpx.Response(200, json=rows_)
            if path.endswith("/smart_money_calls"):
                assert req.url.params.get("chain") == "eq.solana"
                return httpx.Response(200, json=list(fibbot or []))
        return httpx.Response(404)
    return httpx.MockTransport(h)
