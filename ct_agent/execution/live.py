"""Live execution on Pons V2 bonding curves. DISABLED BY DEFAULT.

Refuses to sign unless every gate passes:
  * CT_TRADING_MODE=live
  * CT_LIVE_EXECUTION_REVIEWED=true (set only after a human code review)
  * a trading wallet key is configured (never the owner/treasury key)
  * kill switch off, risk limits pass
  * chain id is Robinhood Chain
  * a fresh quote from onchain reserves, a slippage bound, a fee/tax
    estimate, a balance/allowance check, and an eth_call simulation of the
    exact transaction all succeed

Every submission, fill, partial fill (refund) and failure is journalled.
Graduated coins trade in the ETH pool Pons seeded (Uniswap V4, see v4.py), with the
same gates: a fresh V4Quoter quote, a slippage floor, a simulation of the exact call.
Coins paired with anything but ETH are refused.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from eth_account import Account

from ..chain.abi import checksum, decode_output, encode_call, topic
from ..chain.pons import CURVE_BUY, CURVE_SELL, PonsV2
from ..chain.rpc import RpcClient, RpcError
from ..config import Settings, TradingMode
from ..db import journal_append
from .pricing import curve_buy, curve_sell
from .risk import RiskManager
from . import v4

log = logging.getLogger(__name__)
CURVE_BUY_REFUNDED = topic("CurveBuyRefunded(address,uint256)")
MAX_UINT = 2**256 - 1


class LiveTradingBlocked(RuntimeError):
    pass


@dataclass
class LiveFill:
    tx_hash: str
    side: str
    quote_amount: int
    token_amount: int
    refund: int
    status: str  # filled | partial


class LiveExecutor:
    def __init__(self, settings: Settings, rpc: RpcClient, pons: PonsV2, conn: sqlite3.Connection,
                 risk: RiskManager):
        self.s, self.rpc, self.pons, self.conn, self.risk = settings, rpc, pons, conn, risk
        self._acct = Account.from_key(settings.trading_wallet_private_key) \
            if settings.trading_wallet_private_key else None

    @property
    def wallet(self) -> str | None:
        return self._acct.address if self._acct else None

    def blockers(self) -> list[str]:
        b = []
        if self.s.trading_mode != TradingMode.LIVE:
            b.append("trading mode is paper")
        if not self.s.live_execution_reviewed:
            b.append("live execution adapter not marked reviewed")
        if not self._acct:
            b.append("no trading wallet key")
        killed, why = self.risk.kill_active()
        if killed:
            b.append(why)
        return b

    def _assert_ready(self) -> None:
        b = self.blockers()
        if b:
            raise LiveTradingBlocked("; ".join(b))
        if self.rpc.chain_id() != self.s.chain_id:
            raise LiveTradingBlocked("connected RPC is not Robinhood Chain")

    # --- buy ----------------------------------------------------------------

    def buy_on_curve(self, *, token: str, curve: str, quote_in_wei: int, fee_bps: int, tax_bps: int) -> LiveFill:
        self._assert_ready()
        q_res, t_res = self.pons.curve_reserves(curve)
        est = curve_buy(quote_in_wei, q_res, t_res, fee_bps, tax_bps)
        if est.price_impact_bps > self.s.max_slippage_bps:
            raise LiveTradingBlocked(f"price impact {est.price_impact_bps} bps > {self.s.max_slippage_bps}")
        if fee_bps + tax_bps > self.s.max_trade_tax_bps:
            raise LiveTradingBlocked(f"fee+tax {fee_bps + tax_bps} bps too high")
        min_out = int(est.tokens_out * 10**18 * (1 - self.s.max_slippage_bps / 10_000))
        data = encode_call("buy(uint256,uint256,address)", quote_in_wei, min_out, self.wallet)
        return self._execute(token, curve, "buy", data, value=quote_in_wei,
                             checklist={"quote": est.__dict__, "min_out": min_out})

    # --- sell ---------------------------------------------------------------

    def sell_on_curve(self, *, token: str, curve: str, tokens_in_wei: int, fee_bps: int, tax_bps: int) -> LiveFill:
        self._assert_ready()
        bal = self.pons.erc20_balance(token, self.wallet)
        if bal < tokens_in_wei:
            raise LiveTradingBlocked(f"token balance {bal} < {tokens_in_wei}")
        if self.pons.erc20_allowance(token, self.wallet, curve) < tokens_in_wei:
            approve = encode_call("approve(address,uint256)", checksum(curve), tokens_in_wei)
            self._send(token, approve, 0, "approve", {"spender": curve, "amount": tokens_in_wei})
        q_res, t_res = self.pons.curve_reserves(curve)
        est = curve_sell(tokens_in_wei, q_res, t_res, fee_bps, tax_bps)
        min_out = int(est.quote_in * 10**18 * (1 - self.s.max_slippage_bps / 10_000))
        data = encode_call("sell(uint256,uint256,address)", tokens_in_wei, min_out, self.wallet)
        return self._execute(token, curve, "sell", data, value=0,
                             checklist={"quote": est.__dict__, "min_out": min_out})

    # --- graduated: the coin's ETH pool on Uniswap V4 -------------------------

    def eth_pool(self, token: str) -> v4.EthPool:
        """The pool Pons seeded for this coin, refused unless it's paired with ETH and graduated."""
        from ..chain.pons import Phase
        rec = self.pons.launch_record(token)
        if not rec.exists or rec.phase != Phase.POOL_CREATED:
            raise LiveTradingBlocked("not a graduated Pons coin")
        if rec.pair_token.lower() != v4.ZERO_ADDRESS:
            raise LiveTradingBlocked("pool is not paired with ETH")
        return v4.EthPool(rec.token, rec.pool_fee, rec.tick_spacing, self.pons.meme_hook())

    def _quote(self, pool: v4.EthPool, buy: bool, amount_in: int) -> int:
        try:
            return v4.decode_quote(self.rpc.eth_call(self.s.uniswap_v4_quoter, v4.quote_calldata(pool, buy, amount_in)))
        except (RpcError, ValueError) as exc:
            raise LiveTradingBlocked(f"no V4 quote: {exc}") from exc

    def swap_on_pool(self, *, token: str, side: str, amount_in_wei: int) -> LiveFill:
        """Buy with `amount_in_wei` ETH, or sell `amount_in_wei` of the coin, in its ETH pool."""
        self._assert_ready()
        pool = self.eth_pool(token)
        router = checksum(self.s.uniswap_universal_router)
        quoted = self._quote(pool, side == "buy", amount_in_wei)
        min_out = int(quoted * (1 - self.s.max_slippage_bps / 10_000))
        deadline = int(time.time()) + 120
        if side == "buy":
            data, value = v4.buy_calldata(pool, amount_in_wei, min_out, self.wallet, deadline), amount_in_wei
        else:
            bal = self.pons.erc20_balance(token, self.wallet)
            if bal < amount_in_wei:
                raise LiveTradingBlocked(f"token balance {bal} < {amount_in_wei}")
            self._permit2_allow(token, router, amount_in_wei)
            data, value = v4.sell_calldata(pool, amount_in_wei, min_out, self.wallet, deadline), 0
        checklist = {"venue": "v4_pool", "quote_out": quoted, "min_out": min_out, "amount_in": amount_in_wei,
                     "pool": list(pool.key)}
        before = (self.rpc.get_balance(self.wallet), self.pons.erc20_balance(token, self.wallet))
        rc = self._execute(token, router, side, data, value, checklist, parse=False)
        return self._pool_fill(token, side, rc, before, amount_in_wei)

    def _permit2_allow(self, token: str, router: str, amount: int) -> None:
        """The router pulls the coin through Permit2: the coin approves Permit2 once, then Permit2
        approves the router for this coin (30 days, redone when it runs low or expires)."""
        permit2 = checksum(self.s.permit2)
        if self.pons.erc20_allowance(token, self.wallet, permit2) < amount:
            self._send(token, encode_call("approve(address,uint256)", permit2, MAX_UINT), 0, "approve_permit2",
                       {"spender": permit2})
        amt, exp, _ = decode_output(["uint160", "uint48", "uint48"], self.rpc.eth_call(
            permit2, encode_call("allowance(address,address,address)", self.wallet, checksum(token), router)))
        if int(amt) < amount or int(exp) < time.time() + 600:
            data = encode_call("approve(address,address,uint160,uint48)", checksum(token), router, 2**160 - 1,
                               int(time.time()) + 30 * 86400)
            self._send(permit2, data, 0, "permit2_approve", {"token": token, "spender": router})

    def _pool_fill(self, token: str, side: str, rc: dict[str, Any], before: tuple[int, int], amount_in: int) -> LiveFill:
        """What actually changed hands, from the wallet's balances around the swap (the hook's tax
        is not in the pool's Swap event). ETH received on a sell adds back the gas the swap burned."""
        tokens_now = self.pons.erc20_balance(token, self.wallet)
        if side == "buy":
            quote, tokens = amount_in, tokens_now - before[1]
        else:
            gas = int(rc.get("gasUsed", "0x0"), 16) * int(rc.get("effectiveGasPrice", "0x0"), 16)
            quote, tokens = self.rpc.get_balance(self.wallet) - before[0] + gas, before[1] - tokens_now
        fill = LiveFill(rc["transactionHash"], side, int(quote), int(tokens), 0, "filled")
        journal_append(self.conn, "live_fill", {
            "onchain": True, "paper": False, "venue": "v4_pool", "tx_hash": fill.tx_hash, "side": side,
            "quote_amount": fill.quote_amount, "token_amount": fill.token_amount, "token": token}, ref=token, mode="live")
        return fill

    # --- internals ----------------------------------------------------------

    def _execute(self, token: str, to: str, side: str, data: str, value: int,
                 checklist: dict[str, Any], parse: bool = True) -> Any:
        if value and self.rpc.get_balance(self.wallet) < value * 12 // 10:
            raise LiveTradingBlocked("wallet ETH balance too low for size plus gas")
        try:  # simulate the exact call
            self.rpc.eth_call(to, data, sender=self.wallet, value=value)
        except RpcError as exc:
            journal_append(self.conn, "live_failed", {"stage": "simulation", "side": side, "error": str(exc),
                                                      **checklist}, ref=token, mode="live")
            raise LiveTradingBlocked(f"simulation reverted: {exc}") from exc
        receipt = self._send(to, data, value, side, checklist)
        return self._parse_fill(token, side, receipt) if parse else receipt

    def _send(self, to: str, data: str, value: int, label: str, meta: dict[str, Any]) -> dict[str, Any]:
        txd = {"from": self.wallet, "to": checksum(to), "data": data, "value": hex(value)}
        gas = int(self.rpc.estimate_gas(txd) * 1.3)
        tx = {"to": checksum(to), "data": data, "value": value, "gas": gas,
              "gasPrice": int(self.rpc.gas_price() * 1.2), "nonce": self.rpc.nonce(self.wallet),
              "chainId": self.s.chain_id}
        signed = self._acct.sign_transaction(tx)
        raw = signed.raw_transaction.hex()
        tx_hash = self.rpc.send_raw(raw if raw.startswith("0x") else "0x" + raw)
        journal_append(self.conn, "live_submitted", {"label": label, "tx_hash": tx_hash, "to": to, "value": value,
                                                     **meta}, ref=to, mode="live")
        for _ in range(120):
            rc = self.rpc.receipt(tx_hash)
            if rc:
                if int(rc["status"], 16) != 1:
                    journal_append(self.conn, "live_failed", {"stage": "mined", "label": label, "tx_hash": tx_hash},
                                   ref=to, mode="live")
                    raise LiveTradingBlocked(f"{label} transaction reverted: {tx_hash}")
                return rc
            time.sleep(1)
        journal_append(self.conn, "live_failed", {"stage": "receipt_timeout", "label": label, "tx_hash": tx_hash},
                       ref=to, mode="live")
        raise LiveTradingBlocked(f"no receipt for {tx_hash}; state unknown, check explorer before retrying")

    def _parse_fill(self, token: str, side: str, rc: dict[str, Any]) -> LiveFill:
        quote = tokens = refund = 0
        want = CURVE_BUY if side == "buy" else CURVE_SELL
        for lg in rc.get("logs", []):
            t0 = lg["topics"][0].lower()
            if t0 == want:
                a, b, _, _ = decode_output(["uint256"] * 4, lg["data"])
                quote, tokens = (a, b) if side == "buy" else (b, a)
            elif t0 == CURVE_BUY_REFUNDED:
                refund = int(decode_output(["uint256"], lg["data"])[0])
        status = "partial" if refund else "filled"
        fill = LiveFill(rc["transactionHash"], side, int(quote), int(tokens), refund, status)
        journal_append(self.conn, "live_partial_fill" if refund else "live_fill", {
            "onchain": True, "paper": False, "tx_hash": fill.tx_hash, "side": side, "quote_amount": fill.quote_amount,
            "token_amount": fill.token_amount, "refund": refund, "token": token}, ref=token, mode="live")
        return fill
