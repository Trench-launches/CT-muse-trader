"""Live execution on Solana through Jupiter. DISABLED BY DEFAULT.

Refuses to sign unless every gate passes:
  * CT_TRADING_MODE=live
  * CT_LIVE_EXECUTION_REVIEWED=true (set only after a human code review)
  * a valid trading wallet key is configured (never the owner's or the treasury's)
  * the kill switch is off (risk limits are checked before a trade gets here)
  * the RPC is Solana mainnet (its genesis hash)
  * a fresh Jupiter order for this exact swap, with its price impact inside max_slippage_bps and the same
    bound as its slippage limit

Jupiter builds the transaction (Swap V2 `order`), VIGIL signs it with the trading key, and Jupiter lands it
(`execute`). The fill is what Jupiter reports changed hands. Every submission, fill and failure is journalled.
`CT_LIVE_VENUE=gmgn` sends the same swaps through GMGN's trading API instead (see gmgn.py).
"""
from __future__ import annotations

import base64
import logging
import sqlite3
from dataclasses import dataclass
from typing import Any

import httpx
from solders.transaction import VersionedTransaction

from ..chain.keys import MAINNET_GENESIS, SOL_MINT, is_pubkey, load_keypair
from ..chain.rpc import RpcError
from ..chain.solana import SolanaRpc
from ..config import Settings, TradingMode
from ..db import journal_append
from .jupiter import Jupiter, JupiterError, sign
from .pricing import impact_bps
from .risk import RiskManager

log = logging.getLogger(__name__)
FEE_RESERVE_LAMPORTS = 10_000_000  # 0.01 SOL kept for network fees and a new token account's rent


class LiveTradingBlocked(RuntimeError):
    pass


@dataclass
class LiveFill:
    tx_hash: str        # the transaction signature
    side: str
    quote_amount: int   # lamports paid (buy) or received (sell)
    token_amount: int   # token base units received (buy) or sold (sell)
    refund: int = 0
    status: str = "filled"


class LiveExecutor:
    def __init__(self, settings: Settings, rpc: SolanaRpc, jupiter: Jupiter, conn: sqlite3.Connection,
                 risk: RiskManager):
        self.s, self.rpc, self.jupiter, self.conn, self.risk = settings, rpc, jupiter, conn, risk
        self._key_error = ""
        self._kp = None
        if settings.trading_wallet_private_key:
            try:
                self._kp = load_keypair(settings.trading_wallet_private_key)
            except (ValueError, TypeError) as exc:
                self._key_error = f"trading wallet key is not a valid Solana key ({type(exc).__name__})"
        self._gmgn: Any = None  # GMGN's trading API client, made on first use (live_venue=gmgn)
        self._mainnet: bool | None = None

    @property
    def wallet(self) -> str | None:
        """The trading wallet's public address (base58). Never the key."""
        return str(self._kp.pubkey()) if self._kp else None

    def blockers(self) -> list[str]:
        b = []
        if self.s.trading_mode != TradingMode.LIVE:
            b.append("trading mode is paper")
        if not self.s.live_execution_reviewed:
            b.append("live execution adapter not marked reviewed")
        if self._key_error:
            b.append(self._key_error)
        elif not self._kp:
            b.append("no trading wallet key")
        killed, why = self.risk.kill_active()
        if killed:
            b.append(why)
        return b

    def _assert_ready(self) -> None:
        b = self.blockers()
        if b:
            raise LiveTradingBlocked("; ".join(b))
        if self._mainnet is None:
            try:
                self._mainnet = self.rpc.genesis_hash() == MAINNET_GENESIS
            except RpcError as exc:
                raise LiveTradingBlocked(f"can't reach the Solana RPC: {exc}") from exc
        if not self._mainnet:
            raise LiveTradingBlocked("connected RPC is not Solana mainnet")

    # --- quotes ---------------------------------------------------------------

    def quote_sell(self, token: str, amount_base: int) -> int:
        """Lamports that selling `amount_base` of the coin would return right now (a Jupiter quote: fees and price
        impact in). Raises LiveTradingBlocked if there's no route."""
        try:
            return int(self.jupiter.order(token, SOL_MINT, amount_base)["outAmount"])
        except (JupiterError, KeyError, ValueError) as exc:
            raise LiveTradingBlocked(f"no Jupiter quote: {exc}") from exc

    # --- swaps ----------------------------------------------------------------

    def _sellable(self, token: str, want: int) -> int:
        """How much of `want` can be sold. Token amounts are kept as floats, so "sell it all" can come back a
        hair over the exact balance: within a millionth, sell exactly what the wallet holds. More missing than
        that is refused."""
        bal = self.rpc.token_balance(self.wallet, token)
        if bal >= want:
            return want
        if bal >= want - want // 1_000_000 and bal > 0:
            return bal
        raise LiveTradingBlocked(f"token balance {bal} < {want}")

    def swap(self, token: str, side: str, amount_base: int) -> LiveFill:
        """Buy the coin with `amount_base` lamports, or sell `amount_base` of the coin's base units for SOL."""
        if side not in ("buy", "sell"):
            raise ValueError(side)
        self._assert_ready()
        if not is_pubkey(token) or token == SOL_MINT:
            raise LiveTradingBlocked("not a Solana token mint")
        if amount_base <= 0:
            raise LiveTradingBlocked("nothing to swap")
        if side == "sell":
            amount_base = self._sellable(token, amount_base)
        elif self.rpc.get_balance(self.wallet) < amount_base + FEE_RESERVE_LAMPORTS:
            raise LiveTradingBlocked("wallet SOL balance too low for the size plus fees")
        if self.s.live_venue == "gmgn":
            return self._gmgn_swap(token, side, amount_base)
        inp, out = (SOL_MINT, token) if side == "buy" else (token, SOL_MINT)
        meta = {"venue": "jupiter", "side": side, "amount_in": amount_base, "token": token}
        try:
            order = self.jupiter.order(inp, out, amount_base, taker=self.wallet, slippage_bps=self.s.max_slippage_bps)
        except JupiterError as exc:
            journal_append(self.conn, "live_failed", {"stage": "quote", "error": str(exc), **meta}, ref=token, mode="live")
            raise LiveTradingBlocked(f"no Jupiter route: {exc}") from exc
        imp = impact_bps(order)
        checklist = {"request_id": order.get("requestId"), "in_amount": order.get("inAmount"),
                     "quoted_out": order.get("outAmount"), "price_impact_bps": imp, "router": order.get("router"),
                     "slippage_bps": order.get("slippageBps")}
        if imp > self.s.max_slippage_bps:
            journal_append(self.conn, "live_failed", {"stage": "price_impact", **checklist, **meta}, ref=token, mode="live")
            raise LiveTradingBlocked(f"price impact {imp} bps > {self.s.max_slippage_bps}")
        if not order.get("transaction") or not order.get("requestId"):
            why = order.get("errorMessage") or order.get("error") or "no transaction in the order"
            journal_append(self.conn, "live_failed", {"stage": "order", "error": why, **checklist, **meta}, ref=token,
                           mode="live")
            raise LiveTradingBlocked(f"Jupiter didn't build the swap: {why}")
        signed = sign(order["transaction"], self._kp)
        sig = str(VersionedTransaction.from_bytes(base64.b64decode(signed)).signatures[0])
        journal_append(self.conn, "live_submitted", {"label": side, "tx_hash": sig, **checklist, **meta}, ref=token,
                       mode="live")
        try:
            res = self.jupiter.execute(signed, order["requestId"])
        except JupiterError as exc:
            res = self._confirm_after_error(token, sig, exc, meta)
            if res is None:
                raise LiveTradingBlocked(f"swap {sig[:12]}… not confirmed ({exc}); check the explorer before retrying") \
                    from exc
            res = {**res, "totalInputAmount": order["inAmount"], "totalOutputAmount": order["outAmount"]}
        a = int(res.get("totalInputAmount") or res.get("inputAmountResult") or order["inAmount"])
        b = int(res.get("totalOutputAmount") or res.get("outputAmountResult") or order["outAmount"])
        quote, tokens = (a, b) if side == "buy" else (b, a)
        fill = LiveFill(str(res.get("signature") or sig), side, quote, tokens)
        journal_append(self.conn, "live_fill", {
            "onchain": True, "paper": False, "venue": "jupiter", "tx_hash": fill.tx_hash, "side": side,
            "quote_amount": fill.quote_amount, "token_amount": fill.token_amount, "token": token,
            "explorer": f"{self.s.explorer_url}/tx/{fill.tx_hash}"}, ref=token, mode="live")
        return fill

    def _confirm_after_error(self, token: str, sig: str, exc: Exception, meta: dict[str, Any]) -> dict[str, Any] | None:
        """Jupiter's execute failed or timed out: the transaction may still have landed. Ask the chain."""
        try:
            st = (self.rpc.signature_statuses([sig]) or [None])[0]
        except RpcError:
            st = None
        if st and not st.get("err") and st.get("confirmationStatus") in ("confirmed", "finalized"):
            journal_append(self.conn, "live_fill_from_quote", {"tx_hash": sig, "jupiter_error": str(exc), **meta},
                           ref=token, mode="live")
            return {"signature": sig}
        journal_append(self.conn, "live_failed", {"stage": "execute", "tx_hash": sig, "error": str(exc),
                                                  "chain_status": st, **meta}, ref=token, mode="live")
        return None

    def _gmgn_swap(self, token: str, side: str, amount_in: int) -> LiveFill:
        """The same swap through GMGN's trading API: GMGN routes and signs it from the wallet bound to the
        API key (it must be this trading wallet), and the order is polled until it confirms onchain."""
        from .gmgn import DONE, NATIVE, GmgnClient, GmgnError
        s = self.s
        if not (s.gmgn_api_key and s.gmgn_private_key):
            raise LiveTradingBlocked("GMGN venue chosen but its API key or signing key is missing")
        before = (self.rpc.get_balance(self.wallet), self.rpc.token_balance(self.wallet, token))
        if self._gmgn is None:
            self._gmgn = GmgnClient(s.gmgn_api_key, s.gmgn_private_key, s.gmgn_host)
        meta = {"venue": "gmgn", "side": side, "amount_in": amount_in, "token": token}
        inp, out = (NATIVE, token) if side == "buy" else (token, NATIVE)
        try:
            sub = self._gmgn.swap(chain=s.gmgn_chain, wallet=self.wallet, input_token=inp, output_token=out,
                                  amount=amount_in, slippage_pct=s.gmgn_slippage_pct or None)
        except (GmgnError, httpx.HTTPError) as exc:
            journal_append(self.conn, "live_failed", {"stage": "submit", "error": str(exc), **meta}, ref=token, mode="live")
            raise LiveTradingBlocked(f"GMGN swap refused: {exc}") from exc
        oid = sub.get("order_id")
        journal_append(self.conn, "live_submitted", {"label": side, "order_id": oid, "tx_hash": sub.get("hash"), **meta},
                       ref=token, mode="live")
        try:
            o = sub if sub.get("status") in DONE else self._gmgn.wait(s.gmgn_chain, oid)
        except (GmgnError, httpx.HTTPError) as exc:
            journal_append(self.conn, "live_failed", {"stage": "confirm", "order_id": oid, "error": str(exc), **meta},
                           ref=token, mode="live")
            raise LiveTradingBlocked(f"GMGN order {oid} state unknown ({exc}); check GMGN before retrying") from exc
        if o.get("status") != "confirmed":
            journal_append(self.conn, "live_failed", {"stage": "mined", "order_id": oid, "status": o.get("status"),
                                                      "error": o.get("error_status") or o.get("error_code"), **meta},
                           ref=token, mode="live")
            raise LiveTradingBlocked(f"GMGN order {oid} {o.get('status')}: {o.get('error_status') or o.get('error_code')}")
        rep = o.get("report") or {}
        if rep.get("input_amount") and rep.get("output_amount"):
            a, b = int(rep["input_amount"]), int(rep["output_amount"])
            quote, tokens = (a, b) if side == "buy" else (b, a)
        else:  # no execution report: read what changed hands from the wallet (sell proceeds net of fees)
            tokens_now = self.rpc.token_balance(self.wallet, token)
            quote, tokens = (amount_in, tokens_now - before[1]) if side == "buy" else \
                (self.rpc.get_balance(self.wallet) - before[0], before[1] - tokens_now)
        fill = LiveFill(o.get("hash") or sub.get("hash") or "", side, int(quote), int(tokens))
        journal_append(self.conn, "live_fill", {
            "onchain": True, "paper": False, "venue": "gmgn", "order_id": oid, "tx_hash": fill.tx_hash, "side": side,
            "quote_amount": fill.quote_amount, "token_amount": fill.token_amount, "token": token}, ref=token, mode="live")
        return fill
