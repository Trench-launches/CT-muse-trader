"""What VIGIL checks about a Solana coin before it trades it (replaces the Pons launch reader).

A coin is its SPL mint. VIGIL reads the mint account (decimals, supply, who can still mint or freeze, Token-2022
extensions), the biggest holders (counting only wallets: bonding curves, pools and other program-owned accounts
are left out) and the coin's own metadata (name, image, description, links) for its story.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from .keys import INCINERATOR, SYSTEM_PROGRAM, TOKEN_2022_PROGRAM, TOKEN_PROGRAM, is_pubkey
from .rpc import RpcError
from .solana import SolanaRpc

log = logging.getLogger(__name__)

# Token-2022 extensions that let someone take, block or tax holders' coins
DANGEROUS_EXTENSIONS = {"permanentDelegate": "a permanent delegate can move anyone's coins",
                        "transferHook": "every transfer runs someone's program",
                        "nonTransferable": "the coin can't be transferred",
                        "pausableConfig": "transfers can be paused",
                        "defaultAccountState": "new holders can start frozen"}


@dataclass
class MintInfo:
    mint: str
    exists: bool
    program: str = ""
    decimals: int = 0
    supply: int = 0                 # base units
    mint_authority: str | None = None
    freeze_authority: str | None = None
    extensions: list[dict[str, Any]] = field(default_factory=list)

    @property
    def is_token_2022(self) -> bool:
        return self.program == TOKEN_2022_PROGRAM

    def transfer_fee_bps(self) -> int:
        for e in self.extensions:
            if e.get("extension") == "transferFeeConfig":
                st = e.get("state") or {}
                fee = st.get("newerTransferFee") or st.get("olderTransferFee") or {}
                return int(fee.get("transferFeeBasisPoints") or 0)
        return 0


def mint_info(rpc: SolanaRpc, mint: str) -> MintInfo:
    if not is_pubkey(mint):
        return MintInfo(mint, False)
    acc = rpc.account_info(mint)
    data = (acc or {}).get("data") or {}
    parsed = data.get("parsed") if isinstance(data, dict) else None
    if not acc or acc.get("owner") not in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM) or not parsed or parsed.get("type") != "mint":
        return MintInfo(mint, False, program=(acc or {}).get("owner", ""))
    info = parsed.get("info") or {}
    return MintInfo(mint, True, program=acc["owner"], decimals=int(info.get("decimals") or 0),
                    supply=int(info.get("supply") or 0), mint_authority=info.get("mintAuthority"),
                    freeze_authority=info.get("freezeAuthority"), extensions=list(info.get("extensions") or []))


def safety_blockers(m: MintInfo, max_transfer_fee_bps: int = 0) -> list[str]:
    """Why a coin is unsafe to hold, in plain words. Empty = nothing found."""
    if not m.exists:
        return ["not a Solana token mint"]
    out = []
    if m.mint_authority:
        out.append("its creator can still mint more coins")
    if m.freeze_authority:
        out.append("its creator can freeze holders' coins")
    for e in m.extensions:
        name = e.get("extension")
        if name in DANGEROUS_EXTENSIONS:
            if name == "transferHook" and not (e.get("state") or {}).get("programId"):
                continue  # an unset hook does nothing
            if name == "defaultAccountState" and (e.get("state") or {}).get("accountState") != "frozen":
                continue
            out.append(DANGEROUS_EXTENSIONS[name])
    fee = m.transfer_fee_bps()
    if fee > max_transfer_fee_bps:
        out.append(f"a {fee / 100:g}% tax on every transfer")
    return out


def holder_facts(rpc: SolanaRpc, mint: str, *, supply: int, creator: str | None = None) -> dict[str, Any]:
    """Shares of supply held by the biggest wallets. Token accounts owned by programs (the pump.fun bonding
    curve, PumpSwap/Raydium/Meteora pools, lockers) and burn addresses are not holders and are left out."""
    if supply <= 0:
        return {"error": "no supply"}
    largest = rpc.largest_accounts(mint)
    token_accts = [a["address"] for a in largest]
    infos = rpc.multiple_accounts(token_accts)
    owner_of: dict[str, str] = {}
    for a, info in zip(token_accts, infos):
        try:
            owner_of[a] = info["data"]["parsed"]["info"]["owner"]
        except (TypeError, KeyError):
            pass
    owners = list(dict.fromkeys(owner_of.values()))
    owner_accts = dict(zip(owners, rpc.multiple_accounts(owners, encoding="base64"))) if owners else {}

    def is_wallet(owner: str) -> bool:
        if owner == INCINERATOR:
            return False
        acc = owner_accts.get(owner)
        return acc is None or acc.get("owner") == SYSTEM_PROGRAM  # a wallet is owned by the System Program

    by_owner: dict[str, int] = {}
    for a in largest:
        owner = owner_of.get(a["address"])
        if owner and is_wallet(owner):
            by_owner[owner] = by_owner.get(owner, 0) + int(a["amount"])
    ranked = sorted(by_owner.values(), reverse=True)
    facts = {"top_holder_share": round(ranked[0] / supply, 4) if ranked else 0.0,
             "top10_share": round(sum(ranked[:10]) / supply, 4),
             "wallets_counted": len(ranked)}
    if creator:
        facts["creator_share"] = round(by_owner.get(creator, 0) / supply, 4)
    return facts


def token_meta(rpc: SolanaRpc, mint: str, http: httpx.Client | None = None) -> dict[str, Any]:
    """Name, symbol, image, description and links. From DAS `getAsset` (Helius) and the metadata JSON it
    points to; {} when unavailable. The creator wrote all of it: it is a claim, shown as untrusted text."""
    asset = rpc.get_asset(mint) or {}
    content = asset.get("content") or {}
    md = content.get("metadata") or {}
    out: dict[str, Any] = {"name": md.get("name") or "", "symbol": md.get("symbol") or "",
                           "image": (content.get("links") or {}).get("image") or "",
                           "description": md.get("description") or ""}
    uri = content.get("json_uri") or ""
    if http and uri.startswith("http"):
        try:
            j = http.get(uri, timeout=6).json()
            out["description"] = out["description"] or str(j.get("description") or "")[:600]
            out["image"] = out["image"] or str(j.get("image") or "")
            for k in ("twitter", "telegram", "website"):
                if j.get(k):
                    out[k] = str(j[k])[:200]
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            log.info("metadata json for %s unavailable: %s", mint, exc)
    return {k: v for k, v in out.items() if v}


def verify(rpc: SolanaRpc, mint: str) -> tuple[bool, str]:
    """Is this a real token mint on Solana? (A lead from a post is only a lead until this passes.)"""
    try:
        m = mint_info(rpc, mint)
    except RpcError as exc:
        return False, f"rpc error: {exc}"
    return (True, "token mint") if m.exists else (False, "not a Solana token mint")
