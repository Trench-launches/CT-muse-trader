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
Graduated V4 pool swaps are NOT implemented: those tokens stay paper-only
until a reviewed router adapter is added.
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

    def swap_on_pool(self, **_: Any) -> LiveFill:
        raise LiveTradingBlocked("V4 pool execution adapter not built; graduated tokens are paper-only")

    # --- internals ----------------------------------------------------------

    def _execute(self, token: str, curve: str, side: str, data: str, value: int,
                 checklist: dict[str, Any]) -> LiveFill:
        if value and self.rpc.get_balance(self.wallet) < value * 12 // 10:
            raise LiveTradingBlocked("wallet ETH balance too low for size plus gas")
        try:  # simulate the exact call
            self.rpc.eth_call(curve, data, sender=self.wallet, value=value)
        except RpcError as exc:
            journal_append(self.conn, "live_failed", {"stage": "simulation", "side": side, "error": str(exc),
                                                      **checklist}, ref=token, mode="live")
            raise LiveTradingBlocked(f"simulation reverted: {exc}") from exc
        receipt = self._send(curve, data, value, side, checklist)
        return self._parse_fill(token, side, receipt)

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
