"""Offline fakes that speak the real wire formats.

FakeChain answers JSON-RPC with properly ABI-encoded data, so tests go
through the same encode/decode code as production.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx
from eth_abi import encode
from eth_utils import to_checksum_address

from ct_agent.chain.abi import ZERO_ADDRESS, address_topic, selector
from ct_agent.chain.pons import CURVE_BUY, CURVE_SELL, LAUNCH_SWEPT, POOL_GRADUATED, TOKEN_LAUNCHED

FACTORY = "0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e"
HOOK = "0xE5e702641eA86F4aE6CC3cdAED2b886f976be044"
BLOCK_TIME = 0.1
GENESIS_TS = 1_750_000_000


def addr(n: int) -> str:
    return to_checksum_address("0x" + f"{n:040x}")


@dataclass
class FakeToken:
    token: str
    curve: str
    deployer: str
    pair_token: str = ZERO_ADDRESS
    threshold: int = 4_200_000_000_000_000_000
    real_quote: int = 0
    phase: int = 0
    fee_bps: int = 100
    creator_tax_bps: int = 0
    pool_fee: int = 0
    tick_spacing: int = 200
    name: str = "Test"
    symbol: str = "TST"
    launch_block: int = 0
    description: str = ""
    socials: str = ""


@dataclass
class FakeChain:
    head: int = 1_000_000
    tokens: dict[str, FakeToken] = field(default_factory=dict)
    logs: list[dict[str, Any]] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    can_launch: bool = True

    # --- scenario builders --------------------------------------------------

    def launch(self, t: FakeToken, block: int) -> None:
        t.launch_block = block
        self.tokens[t.token.lower()] = t
        self.logs.append({
            "address": FACTORY,
            "topics": [TOKEN_LAUNCHED, address_topic(t.token), address_topic(t.curve), address_topic(t.deployer)],
            "data": "0x" + encode(["address", "uint256", "uint256"], [t.pair_token, 0, t.threshold]).hex(),
            "blockNumber": hex(block),
            "transactionHash": "0x" + f"{block:064x}",
            "logIndex": "0x0",
        })

    def graduate(self, t: FakeToken, block: int) -> None:
        t.phase = 2
        t.real_quote = t.threshold
        for tp, data in ((LAUNCH_SWEPT, encode(["uint256", "uint256"], [t.threshold, 10**26])),
                         (POOL_GRADUATED, encode(["uint256", "uint256", "uint256"], [1, 10**26, t.threshold]))):
            self.logs.append({"address": FACTORY, "topics": [tp, address_topic(t.token)], "data": "0x" + data.hex(),
                              "blockNumber": hex(block), "transactionHash": "0x" + f"{block:063x}9", "logIndex": "0x1"})

    def trade(self, t: FakeToken, block: int, side: str, quote: int, tokens: int, trader: str,
              log_index: int = 0) -> None:
        a, b = (quote, tokens) if side == "buy" else (tokens, quote)
        fee = quote * t.fee_bps // 10_000
        self.logs.append({
            "address": t.curve,
            "topics": [CURVE_BUY if side == "buy" else CURVE_SELL, address_topic(trader), address_topic(trader)],
            "data": "0x" + encode(["uint256"] * 4, [a, b, fee, 0]).hex(),
            "blockNumber": hex(block),
            "transactionHash": "0x" + f"{block:062x}{log_index:02x}",
            "logIndex": hex(log_index),
        })
        if side == "buy":
            t.real_quote += quote - fee
        else:
            t.real_quote = max(0, t.real_quote - quote)

    # --- rpc ----------------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if isinstance(body, list):
            out = []
            for item in body:
                try:
                    out.append({"jsonrpc": "2.0", "id": item["id"],
                                "result": self._dispatch(item["method"], item["params"])})
                except _Revert as exc:
                    out.append({"jsonrpc": "2.0", "id": item["id"], "error": {"code": 3, "message": str(exc)}})
            return httpx.Response(200, json=out)
        try:
            result = self._dispatch(body["method"], body["params"])
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})
        except _Revert as exc:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                             "error": {"code": 3, "message": f"execution reverted: {exc}"}})

    def _dispatch(self, method: str, params: list[Any]) -> Any:
        self.calls.append(method)
        if method == "eth_chainId":
            return hex(4663)
        if method == "eth_blockNumber":
            return hex(self.head)
        if method == "eth_getBlockByNumber":
            n = int(params[0], 16)
            return {"number": params[0], "timestamp": hex(int(GENESIS_TS + n * BLOCK_TIME))}
        if method == "eth_getCode":
            a = params[0].lower()
            known = a == FACTORY.lower() or a in self.tokens or any(t.curve.lower() == a for t in self.tokens.values())
            return "0x6080" if known else "0x"
        if method == "eth_getLogs":
            return self._logs(params[0])
        if method == "eth_call":
            return self._call(params[0]["to"], params[0]["data"])
        raise _Revert(f"unsupported {method}")

    def _logs(self, f: dict[str, Any]) -> list[dict[str, Any]]:
        lo, hi = int(f["fromBlock"], 16), int(f["toBlock"], 16)
        a = f.get("address")
        addrs = None if a is None else {x.lower() for x in (a if isinstance(a, list) else [a])}
        t0 = f["topics"][0]
        t0s = {t.lower() for t in (t0 if isinstance(t0, list) else [t0])}
        out = []
        for lg in self.logs:
            n = int(lg["blockNumber"], 16)
            if lo <= n <= hi and (addrs is None or lg["address"].lower() in addrs) and lg["topics"][0].lower() in t0s:
                if len(f["topics"]) > 1 and f["topics"][1] and lg["topics"][1].lower() != f["topics"][1].lower():
                    continue
                out.append(lg)
        return out

    def _call(self, to: str, data: str) -> str:
        sel, args = data[:10], data[10:]
        to_l = to.lower()
        enc = lambda types, vals: "0x" + encode(types, vals).hex()  # noqa: E731
        if to_l == FACTORY.lower():
            if sel == selector("getLaunchedToken(address)"):
                a = "0x" + args[-40:]
                t = self.tokens.get(a.lower())
                if not t:
                    rec = [ZERO_ADDRESS] * 5 + [0, 0, 0, 0, False, 0, 0, 0, 0, False]
                else:
                    rec = [t.token, t.curve, t.deployer, t.deployer, t.pair_token, t.threshold, t.pool_fee,
                           t.tick_spacing, t.creator_tax_bps, False, t.phase, 0, 0, 0, True]
                return enc(["(address,address,address,address,address,uint256,uint24,int24,uint16,bool,uint8,"
                            "uint256,uint256,uint256,bool)"], [tuple(rec)])
            if sel == selector("canLaunch(address)"):
                return enc(["bool"], [self.can_launch])
            if sel == selector("launchFee()"):
                return enc(["uint256"], [500_000_000_000_000])
            if sel == selector("snipeTaxStartBps()"):
                return enc(["uint256"], [9_900])
            if sel == selector("snipeTaxSeconds()"):
                return enc(["uint256"], [3])
            if sel == selector("memeHook()"):
                return enc(["address"], [HOOK])
        for t in self.tokens.values():
            if to_l == t.curve.lower():
                vals = {
                    selector("realQuoteReserve()"): (["uint256"], [t.real_quote]),
                    selector("graduationThreshold()"): (["uint256"], [t.threshold]),
                    selector("feeBps()"): (["uint256"], [t.fee_bps]),
                    selector("creatorTaxBps()"): (["uint256"], [t.creator_tax_bps]),
                    selector("readyToGraduate()"): (["bool"], [t.real_quote >= t.threshold and t.phase == 0]),
                    selector("graduated()"): (["bool"], [t.phase != 0]),
                    # phantom 1.68 ETH + real reserve; token side shrinks as quote grows
                    selector("getReserves()"): (["uint256", "uint256"], [
                        1_680_000_000_000_000_000 + t.real_quote,
                        (1_680_000_000_000_000_000 * 10**27) // (1_680_000_000_000_000_000 + t.real_quote)]),
                }
                if sel in vals:
                    return enc(*vals[sel])
            if to_l == t.token.lower():
                vals = {
                    selector("name()"): (["string"], [t.name]),
                    selector("symbol()"): (["string"], [t.symbol]),
                    selector("description()"): (["string"], [t.description]),
                    selector("socials()"): (["string"], [t.socials]),
                    selector("decimals()"): (["uint8"], [18]),
                    selector("totalSupply()"): (["uint256"], [10**27]),
                }
                if sel in vals:
                    return enc(*vals[sel])
        raise _Revert(f"no handler for {to} {sel}")


class _Revert(Exception):
    pass
