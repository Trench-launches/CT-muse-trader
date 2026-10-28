"""The launcher wallet on Solana: put a coin's image and metadata on IPFS, launch it on pump.fun, then split its
creator fees once (the requester's share and VIGIL's) and pay them out.

Sources (checked 2026-10-28):
- Pinata uploads, v3: POST https://uploads.pinata.cloud/v3/files, `Authorization: Bearer <JWT>`, multipart `file` +
  `network=public` (+ optional `name`); the answer is {"data": {"cid", ...}}.
  https://docs.pinata.cloud/api-reference/endpoint/upload-a-file
- PumpPortal Local Transaction API, coin creation: https://pumpportal.fun/creation and
  https://pumpportal.fun/local-trading-api/trading-api . POST /api/trade-local with {publicKey, action:"create",
  tokenMetadata:{name, symbol, uri}, mint, denominatedInSol:"true", amount, slippage, priorityFee, pool:"pump"} returns
  the serialized transaction as raw bytes; it "needs to be signed by mint and creator keypairs" and we send it through
  our own RPC. pump.fun's own /api/ipfs no longer takes uploads (same page), so metadata goes to Pinata and the URI is
  https://ipfs.io/ipfs/<cid>.
- pump.fun creator-fee sharing: https://github.com/pump-fun/pump-public-docs (commit of 2026-10-13)
  docs/instructions/CREATOR_FEE_SHARING.md, docs/instructions/SWEEP_FEES.md, idl/pump_fees.json, idl/pump.json,
  idl/pump_amm.json, and the official TS SDK @pump-fun/pump-sdk@4.1.0 src/sdk.ts + src/pda.ts (same IDLs). Every
  program id, PDA seed, account order, writable/signer flag and discriminator below is copied from those IDLs; each
  discriminator equals sha256("global:<name>")[:8] (checked in the tests).

The fee split is irreversible: update_fee_shares_v2 revokes the config's admin, so it runs once per coin. Until the
encoding has been checked against a real mainnet launch (rollout step 4 of the plan), FEE_PROGRAM_VERIFIED stays
False and set_fee_split/distribute_fees build and *simulate* their transaction but refuse to send it
(FeeSplitUnverified). The coin then keeps 100% of its creator fees on the launcher until the split is set.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import sqlite3
import struct
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlparse

import httpx
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.token.associated import get_associated_token_address
from solders.transaction import VersionedTransaction

from ..chain.keys import is_pubkey, load_keypair
from ..chain.rpc import RpcError
from ..db import journal_append
from ..units import lamports

log = logging.getLogger(__name__)

# --- program ids (pump-public-docs idl/*.json "address", and the account constants in them) ------------------------
PUMP_PROGRAM = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")        # idl/pump.json
PUMP_AMM_PROGRAM = Pubkey.from_string("pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA")    # idl/pump_amm.json
PUMP_FEES_PROGRAM = Pubkey.from_string("pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ")   # idl/pump_fees.json
SYSTEM_PROGRAM = Pubkey.from_string("11111111111111111111111111111111")
TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
TOKEN_2022_PROGRAM = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")  # idl/pump.json create_v2
ASSOCIATED_TOKEN_PROGRAM = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
COMPUTE_BUDGET_PROGRAM = Pubkey.from_string("ComputeBudget111111111111111111111111111111")
MPL_TOKEN_METADATA = Pubkey.from_string("metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s")  # idl/pump.json create
WSOL_MINT = Pubkey.from_string("So11111111111111111111111111111111111111112")         # a SOL coin's quote_mint

# --- Anchor discriminators: sha256("global:<name>")[:8], as listed in the IDLs -------------------------------------
DISC_CREATE_FEE_SHARING_CONFIG = bytes([195, 78, 86, 76, 111, 52, 251, 213])    # pump_fees
DISC_UPDATE_FEE_SHARES_V2 = bytes([111, 251, 49, 6, 78, 78, 106, 18])          # pump_fees
DISC_SWEEP_CREATOR_FEE = bytes([32, 246, 191, 52, 8, 201, 73, 186])            # pump
DISC_DISTRIBUTE_CREATOR_FEES_V2 = bytes([255, 203, 19, 79, 244, 68, 8, 159])   # pump
DISC_BONDING_CURVE = bytes([23, 183, 248, 55, 96, 216, 172, 96])               # pump account BondingCurve
DISC_SHARING_CONFIG = bytes([216, 74, 9, 0, 56, 140, 93, 75])                  # pump_fees account SharingConfig

MAX_SHAREHOLDERS = 10      # update_fee_shares_v2: 1..10 shareholders, no duplicates, each > 0, sum == 10_000
TOTAL_BPS = 10_000

# Flip to True only after the first real launch has shown these instructions are right on mainnet (simulate from
# the journal, then check the sharing_config on Solscan). Settings can also turn it on: launch_fee_split_verified.
# scripts/check_fee_split.py simulates the whole split on live mainnet coins without sending anything: on 2026-10-28
# it came out active, locked and 90/10 on every normal coin tried, once create_fee_sharing_config passed its optional
# accounts. A real launch is still the last check.
FEE_PROGRAM_VERIFIED = False

PUMPPORTAL_SLIPPAGE = 10           # percent, as in PumpPortal's own example
PUMPPORTAL_PRIORITY_FEE = 0.0005   # SOL
# A create with no buy cost its creator 0.0057-0.0073 SOL in 21 mainnet launches on 2026-10-28 (rent for the mint,
# metadata, curve and its ATA, plus fees); about twice that leaves room for the priority fee and PumpPortal's cut.
MAX_CREATE_OVERHEAD = lamports(0.015)  # above the dev buy; a simulation that spends more is refused
MIN_LAUNCHER_RESERVE = lamports(0.01)  # keep this much on the launcher so the fee-split tx (~0.004) can still be paid
MIN_DISTRIBUTE_LAMPORTS = lamports(0.01)  # below this a payout isn't worth the transaction
RENT_EXEMPT_EMPTY = 890_880        # the minimum a 0-byte account (the creator vault) keeps
FEE_TX_COMPUTE_UNITS = 400_000
FEE_TX_MICROLAMPORTS = 100_000     # priority price per CU: 0.00004 SOL on 400k CU

MAX_IMAGE_BYTES = 5_000_000
IMAGE_TYPES = {b"\x89PNG\r\n\x1a\n": ("image/png", "png"), b"\xff\xd8\xff": ("image/jpeg", "jpg"),
               b"GIF87a": ("image/gif", "gif"), b"GIF89a": ("image/gif", "gif")}
# Request images come from X's media CDN. Only those are fetched, so a post can't make the server fetch anything else.
IMAGE_HOST_SUFFIX = "twimg.com"
PINATA_UPLOAD_URL = "https://uploads.pinata.cloud/v3/files"
IPFS_GATEWAY = "https://ipfs.io/ipfs/"

# Programs a PumpPortal create transaction may call at the top level. Anything else and we don't sign it.
CREATE_TX_PROGRAMS = {COMPUTE_BUDGET_PROGRAM, SYSTEM_PROGRAM, PUMP_PROGRAM, PUMP_FEES_PROGRAM, ASSOCIATED_TOKEN_PROGRAM,
                      TOKEN_PROGRAM, TOKEN_2022_PROGRAM, MPL_TOKEN_METADATA}


def image_type(data: bytes) -> tuple[str, str]:
    """(mime, extension) of a coin image, from its first bytes: PNG, JPEG, GIF or WebP only."""
    for magic, (mime, ext) in IMAGE_TYPES.items():
        if data.startswith(magic):
            return mime, ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", "webp"
    raise LaunchBlocked("the coin image isn't a PNG, JPEG, GIF or WebP")


class LaunchBlocked(RuntimeError):
    """Nothing was sent (or what was sent failed): safe to report as not launched."""


class LaunchUnconfirmed(LaunchBlocked):
    """Signed and handed to the RPC but not confirmed: it may still land, so never launch this request again."""

    def __init__(self, message: str, mint: str, signature: str):
        super().__init__(message)
        self.mint, self.signature = mint, signature


class FeeSplitUnverified(LaunchBlocked):
    """The fee-program instructions haven't been checked on mainnet yet: built and simulated, not sent."""

    def __init__(self, message: str, simulation: dict[str, Any] | None = None):
        super().__init__(message)
        self.simulation = simulation or {}


@dataclass
class LaunchResult:
    mint: str
    signature: str
    cost_lamports: int   # launcher balance before minus after: fees, rent, PumpPortal's cut and the dev buy


@dataclass
class Share:
    address: str
    bps: int

    def as_dict(self) -> dict[str, Any]:
        return {"address": self.address, "bps": self.bps}


# --- PDAs (seeds from the IDLs) -------------------------------------------------------------------------------------

def _pda(seeds: list[bytes], program: Pubkey) -> Pubkey:
    return Pubkey.find_program_address(seeds, program)[0]


def _pk(x: str | Pubkey) -> Pubkey:
    return x if isinstance(x, Pubkey) else Pubkey.from_string(x)


def event_authority(program: Pubkey) -> Pubkey:
    return _pda([b"__event_authority"], program)


def pump_global() -> Pubkey:
    return _pda([b"global"], PUMP_PROGRAM)


def bonding_curve_pda(mint: str | Pubkey) -> Pubkey:
    return _pda([b"bonding-curve", bytes(_pk(mint))], PUMP_PROGRAM)


def sharing_config_pda(mint: str | Pubkey) -> Pubkey:
    return _pda([b"sharing-config", bytes(_pk(mint))], PUMP_FEES_PROGRAM)


def creator_vault_pda(creator: str | Pubkey) -> Pubkey:
    """Pump's vault for whoever is bonding_curve.creator (a wallet, or the sharing config once split)."""
    return _pda([b"creator-vault", bytes(_pk(creator))], PUMP_PROGRAM)


def amm_creator_vault_pda(creator: str | Pubkey) -> Pubkey:
    """PumpSwap's coin-creator vault authority (note the underscore in the seed)."""
    return _pda([b"creator_vault", bytes(_pk(creator))], PUMP_AMM_PROGRAM)


def canonical_pool_pda(mint: str | Pubkey, quote_mint: Pubkey = WSOL_MINT) -> Pubkey:
    """The coin's PumpSwap pool once it graduates: pump_amm seeds ["pool", index u16 LE = 0, creator, base, quote],
    with creator = pump's ["pool-authority", mint] (pump-sdk pda.ts canonicalPumpPoolPdaWithQuote)."""
    authority = _pda([b"pool-authority", bytes(_pk(mint))], PUMP_PROGRAM)
    return _pda([b"pool", struct.pack("<H", 0), bytes(authority), bytes(_pk(mint)), bytes(quote_mint)],
                PUMP_AMM_PROGRAM)


def _ata(owner: Pubkey, mint: Pubkey = WSOL_MINT, program: Pubkey = TOKEN_PROGRAM) -> Pubkey:
    return get_associated_token_address(owner, mint, program)


def _ro(k: Pubkey) -> AccountMeta:
    return AccountMeta(k, is_signer=False, is_writable=False)


def _rw(k: Pubkey) -> AccountMeta:
    return AccountMeta(k, is_signer=False, is_writable=True)


def _signer(k: Pubkey, writable: bool = True) -> AccountMeta:
    return AccountMeta(k, is_signer=True, is_writable=writable)


# --- shares ---------------------------------------------------------------------------------------------------------

def check_shares(shares: list[Share] | list[dict[str, Any]]) -> list[Share]:
    """The program's own rules (pump_fees update_fee_shares_v2), checked before anything is built."""
    out = [s if isinstance(s, Share) else Share(str(s["address"]), int(s["bps"])) for s in shares]
    if not 1 <= len(out) <= MAX_SHAREHOLDERS:
        raise LaunchBlocked(f"fee split needs 1 to {MAX_SHAREHOLDERS} shareholders, got {len(out)}")
    if any(not is_pubkey(s.address) for s in out):
        raise LaunchBlocked("fee split has an address that isn't a Solana address")
    if len({s.address for s in out}) != len(out):
        raise LaunchBlocked("fee split names the same address twice")
    if any(s.bps <= 0 or s.bps > TOTAL_BPS for s in out):
        raise LaunchBlocked("every fee share must be above 0 bps")
    if sum(s.bps for s in out) != TOTAL_BPS:
        raise LaunchBlocked(f"fee shares must add up to {TOTAL_BPS} bps, not {sum(s.bps for s in out)}")
    return out


def split_for(requester: str, launcher: str, requester_bps: int) -> list[Share]:
    """Requester first, then VIGIL's launcher. Either side at 0 (or the same wallet) leaves one shareholder."""
    requester_bps = max(0, min(TOTAL_BPS, int(requester_bps)))
    if requester == launcher or requester_bps == TOTAL_BPS:
        return [Share(requester, TOTAL_BPS)]
    if requester_bps == 0:
        return [Share(launcher, TOTAL_BPS)]
    return [Share(requester, requester_bps), Share(launcher, TOTAL_BPS - requester_bps)]


def encode_shareholders(shares: list[Share]) -> bytes:
    """Borsh Vec<Shareholder{address: pubkey, share_bps: u16}>: u32 LE length, then 32 + 2 bytes each."""
    return struct.pack("<I", len(shares)) + b"".join(bytes(_pk(s.address)) + struct.pack("<H", s.bps) for s in shares)


# --- instructions (account lists exactly as in the IDLs; see the module docstring for sources) ---------------------

def sweep_creator_fee_ix(payer: Pubkey, mint: Pubkey, creator: Pubkey) -> Instruction:
    """pump `sweep_creator_fee` (13 accounts, no args): moves the creator fee that v3 trades keep on the curve into
    creator_vault(creator). Harmless when empty; required before the creator changes or the vault pays out
    (6095 CreatorFeesNotSwept). SOL coins pass WSOL as quote mint and the derived (unused) ATAs."""
    curve, vault = bonding_curve_pda(mint), creator_vault_pda(creator)
    return Instruction(PUMP_PROGRAM, DISC_SWEEP_CREATOR_FEE, [
        _signer(payer), _ro(pump_global()), _ro(mint), _ro(WSOL_MINT), _ro(TOKEN_PROGRAM),
        _ro(ASSOCIATED_TOKEN_PROGRAM), _ro(SYSTEM_PROGRAM), _rw(curve), _rw(_ata(curve)), _rw(vault), _rw(_ata(vault)),
        _ro(event_authority(PUMP_PROGRAM)), _ro(PUMP_PROGRAM)])


def create_fee_sharing_config_ix(payer: Pubkey, mint: Pubkey) -> Instruction:
    """pump_fees `create_fee_sharing_config` (no args). Payer must be the coin's creator. The three trailing accounts
    (pool, pump_amm_program, pump_amm_event_authority) are Anchor *optional* accounts, for graduated coins. Optional
    accounts can't be left out: an absent one is passed as the program's own id in its slot. Leaving them out fails
    with AccountNotEnoughKeys (3005) on `pool`, as a mainnet simulation showed on 2026-10-28. We only split coins still
    on their curve, so all three are placeholders."""
    absent = _ro(PUMP_FEES_PROGRAM)
    return Instruction(PUMP_FEES_PROGRAM, DISC_CREATE_FEE_SHARING_CONFIG, [
        _ro(event_authority(PUMP_FEES_PROGRAM)), _ro(PUMP_FEES_PROGRAM), _signer(payer), _ro(pump_global()), _ro(mint),
        _rw(sharing_config_pda(mint)), _ro(SYSTEM_PROGRAM), _rw(bonding_curve_pda(mint)), _ro(PUMP_PROGRAM),
        _ro(event_authority(PUMP_PROGRAM)), absent, absent, absent])


def update_fee_shares_v2_ix(authority: Pubkey, mint: Pubkey, current: list[Pubkey], shares: list[Share]) -> Instruction:
    """pump_fees `update_fee_shares_v2(shareholders)` (19 accounts). Remaining accounts, as pump-sdk 4.1.0 sends them
    for a WSOL quote: the *current* shareholders (writable, they get any pending fees first), then the canonical pool
    (read only; required on a complete curve, harmless on a live one)."""
    cfg = sharing_config_pda(mint)
    pump_vault, amm_vault = creator_vault_pda(cfg), amm_creator_vault_pda(cfg)
    accounts = [
        _ro(event_authority(PUMP_FEES_PROGRAM)), _ro(PUMP_FEES_PROGRAM), _signer(authority), _ro(pump_global()),
        _ro(mint), _rw(cfg), _ro(bonding_curve_pda(mint)), _rw(pump_vault), _rw(_ata(pump_vault)), _ro(SYSTEM_PROGRAM),
        _ro(PUMP_PROGRAM), _ro(event_authority(PUMP_PROGRAM)), _ro(PUMP_AMM_PROGRAM), _ro(event_authority(PUMP_AMM_PROGRAM)),
        _ro(WSOL_MINT), _ro(TOKEN_PROGRAM), _ro(ASSOCIATED_TOKEN_PROGRAM), _rw(amm_vault), _rw(_ata(amm_vault)),
        *[_rw(k) for k in current], _ro(canonical_pool_pda(mint))]
    return Instruction(PUMP_FEES_PROGRAM, DISC_UPDATE_FEE_SHARES_V2 + encode_shareholders(shares), accounts)


def distribute_creator_fees_v2_ix(payer: Pubkey, mint: Pubkey, shareholders: list[Pubkey]) -> Instruction:
    """pump `distribute_creator_fees_v2(initialize_ata: bool)` (12 accounts), then every shareholder (writable) in
    sharing_config order. SOL coins pay lamports straight to each wallet, so initialize_ata is false."""
    cfg = sharing_config_pda(mint)
    vault = creator_vault_pda(cfg)
    return Instruction(PUMP_PROGRAM, DISC_DISTRIBUTE_CREATOR_FEES_V2 + b"\x00", [
        _signer(payer), _ro(mint), _ro(bonding_curve_pda(mint)), _ro(cfg), _rw(vault), _ro(SYSTEM_PROGRAM),
        _ro(event_authority(PUMP_PROGRAM)), _ro(PUMP_PROGRAM), _rw(_ata(vault)), _ro(WSOL_MINT), _ro(TOKEN_PROGRAM),
        _ro(ASSOCIATED_TOKEN_PROGRAM), *[_rw(k) for k in shareholders]])


# --- account decoding (layouts from the IDL types) -------------------------------------------------------------------

def parse_bonding_curve(data: bytes) -> dict[str, Any]:
    """BondingCurve: 5 x u64 reserves/supply, complete bool, creator pubkey, is_mayhem_mode, is_cashback_coin,
    quote_mint pubkey, creator_fee_bps u64, can_edit_creator_fee, is_holder_reward, creator_fee u64, ... Older curves
    are shorter, so the later fields are read only when present (a missing quote mint means SOL)."""
    if data[:8] != DISC_BONDING_CURVE or len(data) < 81:
        raise LaunchBlocked("not a pump.fun bonding curve")
    out: dict[str, Any] = {"complete": data[48] == 1, "creator": str(Pubkey.from_bytes(data[49:81])),
                           "quote_mint": None, "creator_fee": 0}
    if len(data) >= 115:
        q = Pubkey.from_bytes(data[83:115])
        out["quote_mint"] = None if q == Pubkey.default() else str(q)
    if len(data) >= 133:
        out["creator_fee"] = struct.unpack_from("<Q", data, 125)[0]
    return out


def parse_sharing_config(data: bytes) -> dict[str, Any]:
    """SharingConfig: bump u8, version u8, status enum u8 (0 Paused, 1 Active), mint, admin, admin_revoked bool,
    shareholders Vec<Shareholder>."""
    if data[:8] != DISC_SHARING_CONFIG or len(data) < 80:
        raise LaunchBlocked("not a pump.fun sharing config")
    n = struct.unpack_from("<I", data, 76)[0]
    shares = [Share(str(Pubkey.from_bytes(data[80 + 34 * i:112 + 34 * i])),
                    struct.unpack_from("<H", data, 112 + 34 * i)[0]) for i in range(n)]
    return {"version": data[9], "active": data[10] == 1, "mint": str(Pubkey.from_bytes(data[11:43])),
            "admin": str(Pubkey.from_bytes(data[43:75])), "admin_revoked": data[75] == 1, "shareholders": shares}


# --- the launcher ----------------------------------------------------------------------------------------------------

@dataclass
class PumpLauncher:
    settings: Any
    rpc: Any                      # chain.solana.SolanaRpc
    conn: sqlite3.Connection
    http: httpx.Client | None = None
    transport: httpx.BaseTransport | None = None   # tests: an httpx.MockTransport for Pinata, PumpPortal and images
    fee_split_verified: bool | None = None          # None: FEE_PROGRAM_VERIFIED or settings.launch_fee_split_verified
    new_mint: Callable[[], Keypair] = field(default=Keypair)
    last_image_uri: str | None = None              # the image the last upload pinned (its IPFS URL)

    def __post_init__(self) -> None:
        if self.http is None or self.transport is not None:
            self.http = httpx.Client(timeout=30.0, transport=self.transport, follow_redirects=True)
        key = self.settings.launcher_wallet_private_key
        self._kp = load_keypair(key) if key else None

    @property
    def wallet(self) -> str | None:
        return str(self._kp.pubkey()) if self._kp else None

    @property
    def verified(self) -> bool:
        if self.fee_split_verified is not None:
            return self.fee_split_verified
        return FEE_PROGRAM_VERIFIED or bool(getattr(self.settings, "launch_fee_split_verified", False))

    def _ready(self) -> Keypair:
        if not self.settings.launchpad_enabled:
            raise LaunchBlocked("launchpad is off")
        if not self._kp:
            raise LaunchBlocked("no launcher wallet key")
        trading = getattr(self.settings, "trading_wallet_private_key", "")
        if trading:
            try:
                same = load_keypair(trading).pubkey() == self._kp.pubkey()
            except ValueError:
                same = False
            if same:
                raise LaunchBlocked("the launcher wallet must not be the trading wallet")
        return self._kp

    # --- 1. metadata on IPFS ----------------------------------------------------------------------------------------

    def _image(self, url: str) -> tuple[bytes, str, str]:
        u = urlparse(url or "")
        host = (u.hostname or "").lower()
        if u.scheme != "https" or not (host == IMAGE_HOST_SUFFIX or host.endswith("." + IMAGE_HOST_SUFFIX)):
            raise LaunchBlocked("the coin image must be an image attached on X")
        try:
            with self.http.stream("GET", url) as r:
                if r.status_code != 200:
                    raise LaunchBlocked(f"couldn't fetch the coin image (HTTP {r.status_code})")
                if int(r.headers.get("content-length") or 0) > MAX_IMAGE_BYTES:
                    raise LaunchBlocked("the coin image is too big")
                buf = bytearray()
                for chunk in r.iter_bytes():
                    buf += chunk
                    if len(buf) > MAX_IMAGE_BYTES:
                        raise LaunchBlocked("the coin image is too big")
        except httpx.HTTPError as exc:
            raise LaunchBlocked(f"couldn't fetch the coin image: {type(exc).__name__}") from exc
        data = bytes(buf)
        return (data, *image_type(data))

    def _pin(self, name: str, content: bytes, mime: str) -> str:
        jwt = getattr(self.settings, "pinata_jwt", "")
        if not jwt:
            raise LaunchBlocked("no Pinata key for the coin's metadata")
        try:
            r = self.http.post(PINATA_UPLOAD_URL, headers={"Authorization": f"Bearer {jwt}"},
                               data={"network": "public", "name": name}, files={"file": (name, content, mime)})
        except httpx.HTTPError as exc:
            raise LaunchBlocked(f"IPFS upload failed: {type(exc).__name__}") from exc
        if r.status_code != 200:
            raise LaunchBlocked(f"IPFS upload failed (HTTP {r.status_code})")
        try:
            cid = r.json()["data"]["cid"]
        except (ValueError, KeyError, TypeError) as exc:
            raise LaunchBlocked("IPFS upload gave no CID") from exc
        if not isinstance(cid, str) or not cid.isalnum():
            raise LaunchBlocked("IPFS upload gave a bad CID")
        return IPFS_GATEWAY + cid

    def upload_metadata(self, name: str, symbol: str, description: str, image_url: str,
                        socials: dict[str, str] | None = None) -> str:
        """Pin the request's image (attached on X), then the metadata JSON pump.fun reads; returns the metadata URI."""
        return self.upload_metadata_image(name, symbol, description, self._image(image_url)[0], socials)

    def upload_metadata_image(self, name: str, symbol: str, description: str, data: bytes,
                              socials: dict[str, str] | None = None) -> str:
        """The same from image bytes already in hand (ctagent launch-test reads a local file)."""
        socials = socials or {}
        if len(data) > MAX_IMAGE_BYTES:
            raise LaunchBlocked("the coin image is too big")
        mime, ext = image_type(data)
        image = self.last_image_uri = self._pin(f"{symbol.lower()}.{ext}", data, mime)  # the site shows this copy
        meta = {"name": name, "symbol": symbol, "description": description, "image": image,
                "twitter": socials.get("twitter", ""), "telegram": socials.get("telegram", ""),
                "website": socials.get("website", ""), "showName": True}
        return self._pin(f"{symbol.lower()}.json", json.dumps(meta).encode(), "application/json")

    # --- 2. the launch ----------------------------------------------------------------------------------------------

    def _check_create_tx(self, msg: Any, launcher: Pubkey, mint: Pubkey) -> None:
        """We sign a transaction a third party built: it must be paid by us, need exactly our two signatures, and call
        only the programs a pump.fun create uses."""
        keys = list(msg.account_keys)
        n = msg.header.num_required_signatures
        if not keys or keys[0] != launcher:
            raise LaunchBlocked("PumpPortal's transaction isn't paid by the launcher wallet")
        if n != 2 or set(keys[:n]) != {launcher, mint}:
            raise LaunchBlocked("PumpPortal's transaction wants other signers than the launcher and the new mint")
        programs = {keys[ix.program_id_index] for ix in msg.instructions}
        if PUMP_PROGRAM not in programs:
            raise LaunchBlocked("PumpPortal's transaction doesn't call pump.fun")
        odd = programs - CREATE_TX_PROGRAMS
        if odd:
            raise LaunchBlocked(f"PumpPortal's transaction calls an unexpected program: {sorted(map(str, odd))}")

    def _simulate(self, b64: str, watch: str | None = None) -> dict[str, Any]:
        cfg: dict[str, Any] = {"encoding": "base64", "sigVerify": False, "replaceRecentBlockhash": True,
                               "commitment": "confirmed"}
        if watch:
            cfg["accounts"] = {"encoding": "base64", "addresses": [watch]}
        return self.rpc.request("simulateTransaction", [b64, cfg])["value"]

    def launch(self, name: str, symbol: str, uri: str, dev_buy_sol: float,
               on_signed: Callable[[str, str], None] | None = None) -> LaunchResult:
        """Create the coin on pump.fun with the launcher as creator. A fresh mint keypair is made here and its secret
        never leaves this process. `on_signed(mint, signature)` runs just before the transaction goes out, so the
        caller can record it and never launch the same request twice. Raises LaunchBlocked (nothing landed) or
        LaunchUnconfirmed (sent, outcome unknown)."""
        kp = self._ready()
        me = kp.pubkey()
        dev_buy = max(0.0, float(dev_buy_sol or 0.0))
        before = self.rpc.get_balance(str(me))
        if before < lamports(dev_buy) + MAX_CREATE_OVERHEAD + MIN_LAUNCHER_RESERVE:
            raise LaunchBlocked("launcher wallet is low on SOL")
        mint_kp = self.new_mint()
        mint = mint_kp.pubkey()
        body = {"publicKey": str(me), "action": "create", "tokenMetadata": {"name": name, "symbol": symbol, "uri": uri},
                "mint": str(mint), "denominatedInSol": "true", "amount": dev_buy, "slippage": PUMPPORTAL_SLIPPAGE,
                "priorityFee": PUMPPORTAL_PRIORITY_FEE, "pool": "pump"}
        try:
            r = self.http.post(self.settings.pumpportal_url, json=body)
        except httpx.HTTPError as exc:
            raise LaunchBlocked(f"PumpPortal unreachable: {type(exc).__name__}") from exc
        if r.status_code != 200:
            raise LaunchBlocked(f"PumpPortal said HTTP {r.status_code}: {r.text[:200]}")
        try:
            unsigned = VersionedTransaction.from_bytes(r.content)
        except Exception as exc:  # solders raises its own error types for bad bytes
            raise LaunchBlocked("PumpPortal didn't return a transaction") from exc
        self._check_create_tx(unsigned.message, me, mint)
        signers = {me: kp, mint: mint_kp}
        n = unsigned.message.header.num_required_signatures
        signed = VersionedTransaction(unsigned.message, [signers[k] for k in list(unsigned.message.account_keys)[:n]])
        b64 = base64.b64encode(bytes(signed)).decode()
        sig = str(signed.signatures[0])
        meta = {"name": name, "symbol": symbol, "mint": str(mint), "uri": uri, "dev_buy_sol": dev_buy}
        try:
            sim = self._simulate(b64, watch=str(me))
        except RpcError as exc:
            raise LaunchBlocked(f"couldn't simulate the launch: {exc}") from exc
        if sim.get("err"):
            journal_append(self.conn, "launchpad_failed", {"stage": "simulation", "err": sim["err"],
                                                           "logs": (sim.get("logs") or [])[-8:], **meta}, mode="live")
            raise LaunchBlocked(f"launch simulation failed: {sim['err']}")
        after_sim = ((sim.get("accounts") or [None])[0] or {}).get("lamports")
        if after_sim is not None and before - int(after_sim) > lamports(dev_buy) + MAX_CREATE_OVERHEAD:
            raise LaunchBlocked(f"the launch would cost {(before - int(after_sim)) / 1e9:.4f} SOL, more than expected")
        if on_signed:
            on_signed(str(mint), sig)
        journal_append(self.conn, "launchpad_submitted", {"label": "launch", "signature": sig, **meta},
                       ref=str(mint), mode="live")
        try:
            sent = self.rpc.send_transaction(b64)
        except RpcError as exc:
            raise LaunchUnconfirmed(f"send failed, outcome unknown: {exc}", str(mint), sig) from exc
        if sent != sig:
            log.warning("RPC returned signature %s for %s", sent, sig)
        try:
            self.rpc.confirm(sig)
        except RpcError as exc:
            if "failed:" in str(exc):  # landed and failed on chain: nothing was made (fees aside)
                journal_append(self.conn, "launchpad_failed", {"stage": "chain", "signature": sig, "error": str(exc)[:300],
                                                               **meta}, ref=str(mint), mode="live")
                raise LaunchBlocked(str(exc)) from exc
            raise LaunchUnconfirmed(str(exc), str(mint), sig) from exc
        cost = before - self.rpc.get_balance(str(me))
        out = LaunchResult(str(mint), sig, cost)
        journal_append(self.conn, "launchpad_launched", {"signature": sig, "cost_lamports": cost, **meta},
                       ref=str(mint), mode="live")
        return out

    def tx_cost(self, signature: str) -> int | None:
        """What a landed transaction cost the launcher (fee payer, account 0): for launches confirmed after a restart."""
        tx = self.rpc.get_transaction(signature)
        try:
            m = tx["meta"]
            return int(m["preBalances"][0]) - int(m["postBalances"][0])
        except (TypeError, KeyError, IndexError):
            return None

    # --- 3. the creator-fee split -------------------------------------------------------------------------------------

    def _account(self, addr: Pubkey) -> dict[str, Any] | None:
        return self.rpc.account_info(str(addr), encoding="base64")

    @staticmethod
    def _data(acc: dict[str, Any]) -> bytes:
        d = acc.get("data")
        return base64.b64decode(d[0] if isinstance(d, list) else d)

    def curve(self, mint: str) -> dict[str, Any]:
        acc = self._account(bonding_curve_pda(mint))
        if not acc:
            raise LaunchBlocked("no pump.fun bonding curve for this mint")
        if acc.get("owner") != str(PUMP_PROGRAM):
            raise LaunchBlocked("bonding curve isn't owned by pump.fun")
        bc = parse_bonding_curve(self._data(acc))
        if bc["quote_mint"] not in (None, str(WSOL_MINT)):
            raise LaunchBlocked("only SOL-paired coins are supported")
        return bc

    def sharing_config(self, mint: str) -> dict[str, Any] | None:
        acc = self._account(sharing_config_pda(mint))
        if not acc:
            return None
        if acc.get("owner") != str(PUMP_FEES_PROGRAM):
            raise LaunchBlocked("sharing config isn't owned by the pump fee program")
        return parse_sharing_config(self._data(acc))

    def fee_split_tx(self, mint: str, shares: list[Share] | list[dict[str, Any]]) -> tuple[VersionedTransaction, list[Share]] | None:
        """Build (unsent) the split transaction for a coin on its curve, from what's on chain now. None if the split
        is already locked exactly as asked."""
        kp = self._ready()
        me, m = kp.pubkey(), Pubkey.from_string(mint)
        want = check_shares(shares)
        bc = self.curve(mint)
        cfg_addr = str(sharing_config_pda(mint))
        ixs = [set_compute_unit_limit(FEE_TX_COMPUTE_UNITS), set_compute_unit_price(FEE_TX_MICROLAMPORTS)]
        if bc["creator"] == cfg_addr:  # the config exists already (a create went through on an earlier try)
            cfg = self.sharing_config(mint)
            if cfg is None:
                raise LaunchBlocked("curve points at a sharing config that doesn't exist")
            if cfg["admin_revoked"]:
                if [(s.address, s.bps) for s in cfg["shareholders"]] == [(s.address, s.bps) for s in want]:
                    return None
                raise LaunchBlocked("this coin's fee split is already locked, with different shares")
            if cfg["admin"] != str(me):
                raise LaunchBlocked("the launcher isn't this coin's fee-split admin")
            current = [Pubkey.from_string(s.address) for s in cfg["shareholders"]]
            ixs += [sweep_creator_fee_ix(me, m, Pubkey.from_string(cfg_addr))]
        elif bc["creator"] == str(me):
            if bc["complete"]:
                raise LaunchBlocked("the coin has graduated: splitting a graduated coin's fees isn't built")
            current = [me]  # create_fee_sharing_config starts with [(creator, 10_000 bps)]
            ixs += [sweep_creator_fee_ix(me, m, me), create_fee_sharing_config_ix(me, m)]
        else:
            raise LaunchBlocked("the launcher isn't this coin's creator")
        ixs.append(update_fee_shares_v2_ix(me, m, current, want))
        bh = Hash.from_string(self.rpc.latest_blockhash()["blockhash"])
        return VersionedTransaction(MessageV0.try_compile(me, ixs, [], bh), [kp]), want

    def _send_fee_tx(self, tx: VersionedTransaction, label: str, meta: dict[str, Any]) -> str:
        b64 = base64.b64encode(bytes(tx)).decode()
        try:
            sim = self._simulate(b64)
        except RpcError as exc:
            sim = {"err": f"simulation unavailable: {exc}"}
        if not self.verified:
            raise FeeSplitUnverified(
                f"{label}: the pump fee-program instructions aren't verified on mainnet yet, so it wasn't sent "
                f"(simulation: {'ok' if not sim.get('err') else sim['err']})",
                {"err": sim.get("err"), "logs": (sim.get("logs") or [])[-12:], "instructions": _describe(tx)})
        if sim.get("err"):
            journal_append(self.conn, "launchpad_failed", {"stage": "simulation", "label": label, "err": sim["err"],
                                                           "logs": (sim.get("logs") or [])[-8:], **meta}, mode="live")
            raise LaunchBlocked(f"{label} simulation failed: {sim['err']}")
        sig = str(tx.signatures[0])
        journal_append(self.conn, "launchpad_submitted", {"label": label, "signature": sig, **meta},
                       ref=meta.get("mint"), mode="live")
        self.rpc.send_transaction(b64)
        try:
            self.rpc.confirm(sig)
        except RpcError as exc:
            raise LaunchBlocked(f"{label} not confirmed: {exc}") from exc
        return sig

    def set_fee_split(self, mint: str, shares: list[Share] | list[dict[str, Any]]) -> str | None:
        """Lock the coin's creator-fee split: create_fee_sharing_config then update_fee_shares_v2, one transaction,
        signed by the launcher (the coin's creator). Irreversible. Returns the signature, or None when the same split
        is already locked on chain. Raises FeeSplitUnverified while the encoding isn't verified on mainnet."""
        built = self.fee_split_tx(mint, shares)
        if built is None:
            return None
        tx, want = built
        sig = self._send_fee_tx(tx, "fee_split", {"mint": mint, "shares": [s.as_dict() for s in want]})
        cfg = self.sharing_config(mint)  # read it back: what's on chain is what we report
        got = [(s.address, s.bps) for s in (cfg or {}).get("shareholders", [])]
        if not cfg or not cfg["admin_revoked"] or got != [(s.address, s.bps) for s in want]:
            raise LaunchBlocked(f"fee split {sig} confirmed but the config reads {got}")
        journal_append(self.conn, "launchpad_fee_split", {"mint": mint, "signature": sig,
                                                          "shares": [s.as_dict() for s in want]}, ref=mint, mode="live")
        return sig

    # --- 4. paying the shares out -------------------------------------------------------------------------------------

    def pending_fees(self, mint: str) -> int:
        """Lamports waiting for the shareholders: the split's creator vault above rent, plus what's still on the curve."""
        bc = self.curve(mint)
        vault = self.rpc.get_balance(str(creator_vault_pda(sharing_config_pda(mint))))
        return max(0, vault - RENT_EXEMPT_EMPTY) + int(bc.get("creator_fee") or 0)

    def distribute_fees(self, mint: str, min_lamports: int = MIN_DISTRIBUTE_LAMPORTS) -> str | None:
        """Permissionless payout of the curve's creator fees to the split's shareholders (sweep_creator_fee, then
        distribute_creator_fees_v2), paid for by the launcher. None when there's no split yet or too little waiting.
        Fees a graduated coin earns on PumpSwap need transfer_creator_fees_to_pump_v2 first, which isn't built yet."""
        kp = self._ready()
        me, m = kp.pubkey(), Pubkey.from_string(mint)
        cfg_addr = sharing_config_pda(mint)
        bc = self.curve(mint)
        if bc["creator"] != str(cfg_addr):
            return None
        cfg = self.sharing_config(mint)
        if not cfg or not cfg["active"] or not cfg["shareholders"]:
            return None
        waiting = max(0, self.rpc.get_balance(str(creator_vault_pda(cfg_addr))) - RENT_EXEMPT_EMPTY) \
            + int(bc.get("creator_fee") or 0)
        if waiting < min_lamports:
            return None
        holders = [Pubkey.from_string(s.address) for s in cfg["shareholders"]]
        ixs = [set_compute_unit_limit(FEE_TX_COMPUTE_UNITS), set_compute_unit_price(FEE_TX_MICROLAMPORTS),
               sweep_creator_fee_ix(me, m, cfg_addr), distribute_creator_fees_v2_ix(me, m, holders)]
        bh = Hash.from_string(self.rpc.latest_blockhash()["blockhash"])
        tx = VersionedTransaction(MessageV0.try_compile(me, ixs, [], bh), [kp])
        sig = self._send_fee_tx(tx, "distribute", {"mint": mint, "lamports": waiting})
        journal_append(self.conn, "launchpad_distributed", {"mint": mint, "signature": sig, "lamports": waiting,
                                                            "shares": [s.as_dict() for s in cfg["shareholders"]]},
                       ref=mint, mode="live")
        return sig


def _describe(tx: VersionedTransaction) -> list[dict[str, Any]]:
    """The instructions of a transaction, for a human to check against the docs (never the signed bytes: a signed
    transaction in the journal could be sent by anyone who reads it)."""
    msg = tx.message
    keys = list(msg.account_keys)
    return [{"program": str(keys[ix.program_id_index]), "data_hex": bytes(ix.data).hex(),
             "accounts": [{"pubkey": str(keys[i]), "signer": msg.is_signer(i), "writable": msg.is_maybe_writable(i)}
                          for i in bytes(ix.accounts)]} for ix in msg.instructions]


def discriminator(name: str) -> bytes:
    """Anchor's instruction discriminator, for checking the constants above."""
    return hashlib.sha256(f"global:{name}".encode()).digest()[:8]
