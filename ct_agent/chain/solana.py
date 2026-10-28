"""Solana JSON-RPC on top of the shared client (retries, backup node, batching, redacted URLs)."""
from __future__ import annotations

import time
from typing import Any

from .rpc import RpcClient, RpcError

CONFIRMED = {"commitment": "confirmed"}


class SolanaRpc(RpcClient):
    """`backup_url` answers when the main provider is out of retries (e.g. a free-tier rate limit)."""

    def __init__(self, url: str, *, backup_url: str | None = None, **kw: Any):
        super().__init__(url, backup_url=backup_url, **kw)

    def genesis_hash(self) -> str:
        return self.request("getGenesisHash", [])

    def slot(self) -> int:
        return int(self.request("getSlot", [CONFIRMED]))

    def get_balance(self, pubkey: str) -> int:
        """Lamports."""
        return int(self.request("getBalance", [pubkey, CONFIRMED])["value"])

    def account_info(self, pubkey: str, encoding: str = "jsonParsed") -> dict[str, Any] | None:
        return self.request("getAccountInfo", [pubkey, {"encoding": encoding, **CONFIRMED}])["value"]

    def multiple_accounts(self, pubkeys: list[str], encoding: str = "jsonParsed") -> list[dict[str, Any] | None]:
        out: list[dict[str, Any] | None] = []
        for i in range(0, len(pubkeys), 100):  # the RPC caps one call at 100 accounts
            out += self.request("getMultipleAccounts", [pubkeys[i:i + 100], {"encoding": encoding, **CONFIRMED}])["value"]
        return out

    def token_supply(self, mint: str) -> dict[str, Any]:
        """{amount (base units, str), decimals, uiAmount}"""
        return self.request("getTokenSupply", [mint, CONFIRMED])["value"]

    def largest_accounts(self, mint: str) -> list[dict[str, Any]]:
        """The 20 biggest token accounts of a mint: [{address, amount, decimals, uiAmount}]."""
        return self.request("getTokenLargestAccounts", [mint, CONFIRMED])["value"]

    def token_balance(self, owner: str, mint: str) -> int:
        """Everything `owner` holds of `mint`, in base units, across all its token accounts."""
        res = self.request("getTokenAccountsByOwner", [owner, {"mint": mint}, {"encoding": "jsonParsed", **CONFIRMED}])
        return sum(int(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"]) for a in res["value"])

    def latest_blockhash(self) -> dict[str, Any]:
        return self.request("getLatestBlockhash", [CONFIRMED])["value"]

    def send_transaction(self, signed_b64: str, *, skip_preflight: bool = False) -> str:
        return self.request("sendTransaction", [signed_b64, {"encoding": "base64", "skipPreflight": skip_preflight,
                                                             "preflightCommitment": "confirmed", "maxRetries": 3}])

    def simulate(self, signed_b64: str) -> dict[str, Any]:
        return self.request("simulateTransaction", [signed_b64, {"encoding": "base64", "sigVerify": False,
                                                                 "replaceRecentBlockhash": True, **CONFIRMED}])["value"]

    def signature_statuses(self, signatures: list[str]) -> list[dict[str, Any] | None]:
        return self.request("getSignatureStatuses", [signatures, {"searchTransactionHistory": True}])["value"]

    def confirm(self, signature: str, timeout_s: float = 60.0, poll_s: float = 1.5) -> dict[str, Any]:
        """Wait until the transaction is confirmed. Raises RpcError if it failed on chain or never landed."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            st = self.signature_statuses([signature])[0]
            if st:
                if st.get("err"):
                    raise RpcError(f"transaction {signature[:12]}… failed: {st['err']}")
                if st.get("confirmationStatus") in ("confirmed", "finalized"):
                    return st
            time.sleep(poll_s)
        raise RpcError(f"transaction {signature[:12]}… not confirmed within {timeout_s:.0f}s")

    def get_transaction(self, signature: str) -> dict[str, Any] | None:
        return self.request("getTransaction", [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0,
                                                           **CONFIRMED}])

    def get_asset(self, mint: str) -> dict[str, Any] | None:
        """Helius DAS `getAsset` (token metadata, image, links). None on providers without DAS."""
        try:
            return self.request("getAsset", {"id": mint})  # type: ignore[arg-type]
        except RpcError:
            return None
