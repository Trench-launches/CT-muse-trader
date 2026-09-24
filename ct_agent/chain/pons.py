"""Read-only access to Pons V2 on Robinhood Chain.

Signatures come from the published source (ponsdotdev/pons-labs,
contractsV2/src/v2). Nothing here signs or sends a transaction.

GraduationPhase (factory record):
  0 NotGraduated  - trading on the bonding curve
  1 Swept         - curve drained, V4 pool not yet seeded (skip)
  2 PoolCreated   - live, permanently locked Uniswap V4 pool
  3 Rescued       - reserves released manually, terminal (skip)
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import IntEnum
from typing import Any

from eth_abi import encode
from eth_utils import keccak

from .abi import (ZERO_ADDRESS, address_topic, checksum, decode_output, encode_call, topic,
                  topic_to_address)
from .rpc import RpcClient, RpcError


class Phase(IntEnum):
    NOT_GRADUATED = 0
    SWEPT = 1
    POOL_CREATED = 2
    RESCUED = 3


TOKEN_LAUNCHED = topic("TokenLaunched(address,address,address,address,uint256,uint256)")
POOL_GRADUATED = topic("PoolGraduated(address,uint256,uint256,uint256)")
LAUNCH_SWEPT = topic("LaunchSwept(address,uint256,uint256)")
CURVE_BUY = topic("CurveBuy(address,address,uint256,uint256,uint256,uint256)")
CURVE_SELL = topic("CurveSell(address,address,uint256,uint256,uint256,uint256)")

_LAUNCHED_TOKEN_TUPLE = (
    "(address,address,address,address,address,uint256,uint24,int24,uint16,bool,uint8,"
    "uint256,uint256,uint256,bool)"
)


@dataclass(frozen=True)
class LaunchEvent:
    token: str
    curve: str
    deployer: str
    pair_token: str
    launch_config_id: int
    graduation_threshold: int
    block_number: int
    tx_hash: str


@dataclass(frozen=True)
class LaunchRecord:
    token: str
    curve: str
    deployer: str
    creator_fee_recipient: str
    pair_token: str
    graduation_threshold: int
    pool_fee: int
    tick_spacing: int
    creator_tax_bps: int
    buyback_enabled: bool
    phase: Phase
    swept_quote: int
    swept_tokens: int
    swept_at: int
    exists: bool

    @property
    def native_quote(self) -> bool:
        return self.pair_token.lower() == ZERO_ADDRESS


@dataclass(frozen=True)
class CurveState:
    real_quote_reserve: int
    graduation_threshold: int
    fee_bps: int
    creator_tax_bps: int
    ready_to_graduate: bool
    graduated: bool

    @property
    def progress(self) -> float:
        if self.graduation_threshold <= 0:
            return 0.0
        return self.real_quote_reserve / self.graduation_threshold


@dataclass(frozen=True)
class CurveTrade:
    tx_hash: str
    log_index: int
    block_number: int
    curve: str
    side: str
    trader: str
    quote_amount: int
    token_amount: int
    fee: int
    tax: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TokenMeta:
    name: str
    symbol: str
    decimals: int


class PonsV2:
    def __init__(self, rpc: RpcClient, factory: str, *, log_chunk: int = 5_000):
        self.rpc = rpc
        self.factory = checksum(factory)
        self.log_chunk = log_chunk

    # --- factory ------------------------------------------------------------

    def launches_between(self, from_block: int, to_block: int) -> list[LaunchEvent]:
        logs = self.rpc.get_logs_chunked(address=self.factory, topics=[TOKEN_LAUNCHED],
                                         from_block=from_block, to_block=to_block, chunk=self.log_chunk)
        out = []
        for lg in logs:
            pair, cfg, thr = decode_output(["address", "uint256", "uint256"], lg["data"])
            out.append(LaunchEvent(
                token=topic_to_address(lg["topics"][1]),
                curve=topic_to_address(lg["topics"][2]),
                deployer=topic_to_address(lg["topics"][3]),
                pair_token=checksum(pair),
                launch_config_id=int(cfg),
                graduation_threshold=int(thr),
                block_number=int(lg["blockNumber"], 16),
                tx_hash=lg["transactionHash"],
            ))
        return out

    def launch_record(self, token: str) -> LaunchRecord:
        data = self.rpc.eth_call(self.factory, encode_call("getLaunchedToken(address)", checksum(token)))
        (t,) = decode_output([_LAUNCHED_TOKEN_TUPLE], data)
        return LaunchRecord(
            token=checksum(t[0]), curve=checksum(t[1]), deployer=checksum(t[2]),
            creator_fee_recipient=checksum(t[3]), pair_token=checksum(t[4]),
            graduation_threshold=int(t[5]), pool_fee=int(t[6]), tick_spacing=int(t[7]),
            creator_tax_bps=int(t[8]), buyback_enabled=bool(t[9]), phase=Phase(int(t[10])),
            swept_quote=int(t[11]), swept_tokens=int(t[12]), swept_at=int(t[13]), exists=bool(t[14]),
        )

    def can_launch(self, launcher: str) -> bool:
        data = self.rpc.eth_call(self.factory, encode_call("canLaunch(address)", checksum(launcher)))
        return bool(decode_output(["bool"], data)[0])

    def launch_fee(self) -> int:
        return int(decode_output(["uint256"], self.rpc.eth_call(self.factory, encode_call("launchFee()")))[0])

    def _address_getter(self, sig: str) -> str:
        return checksum(decode_output(["address"], self.rpc.eth_call(self.factory, encode_call(sig)))[0])

    def meme_hook(self) -> str:
        if not hasattr(self, "_meme_hook"):
            self._meme_hook = self._address_getter("memeHook()")
        return self._meme_hook

    def pool_manager(self) -> str:
        return self._address_getter("poolManager()")

    def genuine_pool_id(self, rec: LaunchRecord) -> str:
        """Uniswap V4 PoolId of the one pool Pons seeds for this launch:
        keccak256(abi.encode(currency0, currency1, fee, tickSpacing, hooks)).
        Anyone can create other V4 pools for the same token; those are not
        the graduated pool and are rejected by comparing against this id."""
        a, b = sorted([rec.pair_token.lower(), rec.token.lower()], key=lambda x: int(x, 16))
        raw = encode(["address", "address", "uint24", "int24", "address"],
                     [a, b, rec.pool_fee, rec.tick_spacing, self.meme_hook().lower()])
        return "0x" + keccak(raw).hex()

    def snipe_tax(self) -> tuple[int, int]:
        """(start_bps, window_seconds) for launches created now."""
        bps = decode_output(["uint256"], self.rpc.eth_call(self.factory, encode_call("snipeTaxStartBps()")))[0]
        secs = decode_output(["uint256"], self.rpc.eth_call(self.factory, encode_call("snipeTaxSeconds()")))[0]
        return int(bps), int(secs)

    # --- curve --------------------------------------------------------------

    def curve_state(self, curve: str) -> CurveState:
        def u(sig: str) -> int:
            return int(decode_output(["uint256"], self.rpc.eth_call(curve, encode_call(sig)))[0])

        def b(sig: str) -> bool:
            return bool(decode_output(["bool"], self.rpc.eth_call(curve, encode_call(sig)))[0])

        return CurveState(
            real_quote_reserve=u("realQuoteReserve()"),
            graduation_threshold=u("graduationThreshold()"),
            fee_bps=u("feeBps()"),
            creator_tax_bps=u("creatorTaxBps()"),
            ready_to_graduate=b("readyToGraduate()"),
            graduated=b("graduated()"),
        )

    def real_quote_reserves(self, curves: list[str]) -> dict[str, int | None]:
        """Bulk realQuoteReserve() via one JSON-RPC batch per 50 curves."""
        data = encode_call("realQuoteReserve()")
        calls = [("eth_call", [{"to": checksum(c), "data": data}, "latest"]) for c in curves]
        out: dict[str, int | None] = {}
        for c, res in zip(curves, self.rpc.batch(calls)):
            try:
                out[c] = None if isinstance(res, Exception) else int(decode_output(["uint256"], res)[0])
            except ValueError:
                out[c] = None
        return out

    def phase_events(self, from_block: int, to_block: int) -> list[tuple[str, Phase, int]]:
        """(token, new_phase, block) from LaunchSwept / PoolGraduated."""
        logs = self.rpc.get_logs_chunked(address=self.factory, topics=[[LAUNCH_SWEPT, POOL_GRADUATED]],
                                         from_block=from_block, to_block=to_block, chunk=self.log_chunk)
        out = []
        for lg in logs:
            phase = Phase.SWEPT if lg["topics"][0].lower() == LAUNCH_SWEPT else Phase.POOL_CREATED
            out.append((topic_to_address(lg["topics"][1]), phase, int(lg["blockNumber"], 16)))
        return out

    def curve_reserves(self, curve: str) -> tuple[int, int]:
        """(quoteReserve incl. phantom, tradeable tokenReserve): the constant-
        product pair the curve actually prices against."""
        q, t = decode_output(["uint256", "uint256"], self.rpc.eth_call(curve, encode_call("getReserves()")))
        return int(q), int(t)

    def total_supply(self, token: str) -> int:
        return int(decode_output(["uint256"], self.rpc.eth_call(token, encode_call("totalSupply()")))[0])

    def erc20_balance(self, token: str, owner: str) -> int:
        return int(decode_output(["uint256"], self.rpc.eth_call(
            token, encode_call("balanceOf(address)", checksum(owner))))[0])

    def erc20_allowance(self, token: str, owner: str, spender: str) -> int:
        return int(decode_output(["uint256"], self.rpc.eth_call(
            token, encode_call("allowance(address,address)", checksum(owner), checksum(spender))))[0])

    def curve_trades(self, curves: list[str], from_block: int, to_block: int) -> list[CurveTrade]:
        if not curves:
            return []
        logs = self.rpc.get_logs_chunked(address=[checksum(c) for c in curves],
                                         topics=[[CURVE_BUY, CURVE_SELL]],
                                         from_block=from_block, to_block=to_block, chunk=self.log_chunk)
        out = []
        for lg in logs:
            side = "buy" if lg["topics"][0].lower() == CURVE_BUY else "sell"
            a, b_, fee, tax = decode_output(["uint256"] * 4, lg["data"])
            # buy: quoteIn, tokensOut. sell: tokensIn, quoteOut.
            quote_amt, token_amt = (a, b_) if side == "buy" else (b_, a)
            out.append(CurveTrade(
                tx_hash=lg["transactionHash"], log_index=int(lg["logIndex"], 16),
                block_number=int(lg["blockNumber"], 16), curve=checksum(lg["address"]), side=side,
                trader=topic_to_address(lg["topics"][1]), quote_amount=int(quote_amt),
                token_amount=int(token_amt), fee=int(fee), tax=int(tax),
            ))
        return out

    # --- verification -------------------------------------------------------

    def verify_token(self, address: str) -> tuple[bool, str, LaunchRecord | None]:
        """The gate every narrative must pass before being tied to a token:
        the address must be a Pons V2 launch on this chain, with code, whose
        factory record points back at itself."""
        try:
            addr = checksum(address)
        except (ValueError, TypeError):
            return False, "not a valid EVM address", None
        try:
            code = self.rpc.get_code(addr)
        except RpcError as exc:
            return False, f"rpc error: {exc}", None
        if not code or code == "0x":
            return False, "no contract code at this address on Robinhood Chain", None
        try:
            rec = self.launch_record(addr)
        except (RpcError, ValueError) as exc:
            return False, f"factory lookup failed: {exc}", None
        if not rec.exists or rec.token != addr:
            return False, "not a Pons V2 launch", rec
        return True, f"Pons V2 launch, phase {rec.phase.name}", rec

    def graduated_pool_logged(self, token: str, from_block: int, to_block: int) -> bool:
        logs = self.rpc.get_logs_chunked(address=self.factory, topics=[POOL_GRADUATED, address_topic(token)],
                                         from_block=from_block, to_block=to_block, chunk=self.log_chunk)
        return bool(logs)

    # --- erc20 --------------------------------------------------------------

    def token_meta(self, token: str) -> TokenMeta:
        if token.lower() == ZERO_ADDRESS:
            return TokenMeta("Ether", "ETH", 18)

        def s(sig: str) -> str:
            try:
                return str(decode_output(["string"], self.rpc.eth_call(token, encode_call(sig)))[0])
            except Exception:
                return ""

        try:
            dec = int(decode_output(["uint8"], self.rpc.eth_call(token, encode_call("decimals()")))[0])
        except Exception:
            dec = 18
        return TokenMeta(s("name()"), s("symbol()"), dec)
