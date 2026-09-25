"""The launcher wallet onchain: launch a Pons V2 coin, and hand its creator fees to someone else.

Signatures come from the verified PonsV2LaunchFactory source (Sourcify):
  launchToken((name, symbol, logo, description, (twitter, telegram, discord, website, farcaster),
               creatorFeeRecipient, creatorTaxBps, buybackEnabled, expectedEconomics, salt), launchConfigId, pairToken)
  payable, msg.value must equal launchFee(); launchConfigId 0 and pairToken 0x0 (native ETH) are the only config.
  transferCreatorFeeRecipient(token, newRecipient): callable by the current recipient.
Every transaction is simulated first and journalled; this wallet never trades.
"""
from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from eth_abi import encode
from eth_account import Account

from ..chain.abi import ZERO_ADDRESS, checksum, decode_output, encode_call, selector, topic
from ..chain.pons import TOKEN_LAUNCHED, PonsV2
from ..chain.rpc import RpcClient, RpcError
from ..config import Settings
from ..db import journal_append

TOKEN_PARAMS = "(string,string,string,string,(string,string,string,string,string),address,uint16,bool,bytes32,bytes32)"
LAUNCH_SIG = f"launchToken({TOKEN_PARAMS},uint256,address)"
SOCIALS = ("twitter", "telegram", "discord", "website", "farcaster")
FEES_SWEPT = topic("FeesSwept(uint256,uint256,uint256)")  # (protocol, buyback, creator) amounts


class LaunchBlocked(RuntimeError):
    pass


@dataclass
class Launched:
    token: str
    curve: str
    tx_hash: str
    cost_wei: int   # launch fee + gas actually paid


class Launcher:
    def __init__(self, settings: Settings, rpc: RpcClient, pons: PonsV2, conn: sqlite3.Connection):
        self.s, self.rpc, self.pons, self.conn = settings, rpc, pons, conn
        self._acct = Account.from_key(settings.launcher_wallet_private_key) \
            if settings.launcher_wallet_private_key else None

    @property
    def wallet(self) -> str | None:
        return self._acct.address if self._acct else None

    def _ready(self) -> None:
        if not self.s.launchpad_enabled:
            raise LaunchBlocked("launchpad is off")
        if not self._acct:
            raise LaunchBlocked("no launcher wallet key")
        if self.s.trading_wallet_private_key and \
                Account.from_key(self.s.trading_wallet_private_key).address == self.wallet:
            raise LaunchBlocked("the launcher wallet must not be the trading wallet")
        if self.rpc.chain_id() != self.s.chain_id:
            raise LaunchBlocked("connected RPC is not Robinhood Chain")

    def launch_calldata(self, *, name: str, symbol: str, logo: str, description: str, socials: dict[str, str],
                        economics: bytes, salt: bytes) -> str:
        params = (name, symbol, logo, description, tuple(socials.get(k, "") or "" for k in SOCIALS),
                  checksum(self.wallet), self.s.launch_creator_tax_bps, False, economics, salt)
        return selector(LAUNCH_SIG) + encode([TOKEN_PARAMS, "uint256", "address"], [params, 0, ZERO_ADDRESS]).hex()

    def economics(self) -> bytes:
        """The launch terms as the factory states them now, so a launch reverts if they change under us."""
        try:
            out = self.rpc.eth_call(self.pons.factory, encode_call("previewLaunchEconomics(uint256,address)", 0,
                                                                    ZERO_ADDRESS))
            return bytes(decode_output(["bytes32"], out)[0])
        except (RpcError, ValueError):
            return b"\0" * 32  # zero skips the check

    def launch(self, *, name: str, symbol: str, logo: str, description: str, socials: dict[str, str],
               ref: str) -> Launched:
        """Launch the coin with this wallet as its creator-fee recipient. Raises LaunchBlocked if it can't."""
        self._ready()
        if not self.pons.can_launch(self.wallet):
            raise LaunchBlocked("Pons isn't taking launches from this wallet right now")
        fee = self.pons.launch_fee()
        data = self.launch_calldata(name=name, symbol=symbol, logo=logo, description=description, socials=socials,
                                    economics=self.economics(), salt=os.urandom(32))
        rc = self._send(self.pons.factory, data, fee, "launch", {"name": name, "symbol": symbol, "ref": ref})
        token = curve = None
        for lg in rc.get("logs", []):
            if lg["topics"] and lg["topics"][0].lower() == TOKEN_LAUNCHED and lg["address"].lower() == self.pons.factory.lower():
                token, curve = checksum("0x" + lg["topics"][1][-40:]), checksum("0x" + lg["topics"][2][-40:])
        if not token:
            raise LaunchBlocked(f"launch mined but no TokenLaunched event in {rc['transactionHash']}")
        gas = int(rc.get("gasUsed", "0x0"), 16) * int(rc.get("effectiveGasPrice", "0x0"), 16)
        out = Launched(token, curve, rc["transactionHash"], fee + gas)
        journal_append(self.conn, "launchpad_launched", {"ref": ref, "token": token, "curve": curve, "tx_hash": out.tx_hash,
                                                         "cost_wei": str(out.cost_wei), "name": name, "symbol": symbol},
                       ref=token, mode="live")
        return out

    # --- creator fees: read what's built up on the curve, sweep it to the escrow, claim it ------------------
    # Before graduation fees sit on the curve: the creator gets 70% of the base fee (the protocol keeps 30%) plus
    # all of the creator tax. A sweep credits that to the Pons escrow for whoever is the recipient at that moment.

    def pending_creator_wei(self, curve: str) -> int:
        """Creator money built up on the curve and not yet swept."""
        def u(sig: str) -> int:
            return int(decode_output(["uint256"], self.rpc.eth_call(curve, encode_call(sig)))[0])
        share = u("protocolFeeShareBps()")
        return u("quoteFeeBalance()") * (10_000 - share) // 10_000 + u("creatorTaxBalance()")

    def sweep(self, curve: str, ref: str) -> int:
        """Sweep the curve's fees to the escrow; returns the creator amount credited (from FeesSwept)."""
        self._ready()
        rc = self._send(curve, encode_call("sweepFees(uint256)", 0), 0, "sweep", {"curve": curve, "ref": ref})
        got = 0
        for lg in rc.get("logs", []):
            if lg["address"].lower() == curve.lower() and lg["topics"] and lg["topics"][0].lower() == FEES_SWEPT:
                got += int(decode_output(["uint256", "uint256", "uint256"], lg["data"])[2])
        return got

    def escrow_balance(self) -> int:
        return int(decode_output(["uint256"], self.rpc.eth_call(
            self.s.pons_fee_escrow, encode_call("balanceOf(address)", checksum(self.wallet))))[0])

    def claim(self) -> str:
        """Pull this wallet's credited fees out of the escrow (they refill the launch budget)."""
        self._ready()
        return self._send(self.s.pons_fee_escrow, encode_call("claim()"), 0, "claim", {})["transactionHash"]

    def hand_over(self, token: str, to: str) -> str:
        """Make `to` the coin's creator-fee recipient. Only the current recipient (this wallet) can."""
        self._ready()
        data = encode_call("transferCreatorFeeRecipient(address,address)", checksum(token), checksum(to))
        rc = self._send(self.pons.factory, data, 0, "hand_over", {"token": token, "to": to})
        return rc["transactionHash"]

    def _send(self, to: str, data: str, value: int, label: str, meta: dict[str, Any]) -> dict[str, Any]:
        if value and self.rpc.get_balance(self.wallet) < value * 12 // 10 + 10**15:
            raise LaunchBlocked("launcher wallet is low on ETH")
        try:  # simulate the exact call first
            self.rpc.eth_call(to, data, sender=self.wallet, value=value)
        except RpcError as exc:
            journal_append(self.conn, "launchpad_failed", {"stage": "simulation", "label": label, "error": str(exc)[:300],
                                                           **meta}, mode="live")
            raise LaunchBlocked(f"{label} simulation reverted: {exc}") from exc
        txd = {"from": self.wallet, "to": checksum(to), "data": data, "value": hex(value)}
        tx = {"to": checksum(to), "data": data, "value": value, "gas": int(self.rpc.estimate_gas(txd) * 1.3),
              "gasPrice": int(self.rpc.gas_price() * 1.2), "nonce": self.rpc.nonce(self.wallet), "chainId": self.s.chain_id}
        raw = self._acct.sign_transaction(tx).raw_transaction.hex()
        tx_hash = self.rpc.send_raw(raw if raw.startswith("0x") else "0x" + raw)
        journal_append(self.conn, "launchpad_submitted", {"label": label, "tx_hash": tx_hash, "value": value, **meta},
                       mode="live")
        for _ in range(120):
            rc = self.rpc.receipt(tx_hash)
            if rc:
                if int(rc["status"], 16) != 1:
                    journal_append(self.conn, "launchpad_failed", {"stage": "mined", "label": label, "tx_hash": tx_hash,
                                                                   **meta}, mode="live")
                    raise LaunchBlocked(f"{label} transaction reverted: {tx_hash}")
                return rc
            time.sleep(1)
        raise LaunchBlocked(f"no receipt for {tx_hash}; check the explorer before retrying")
