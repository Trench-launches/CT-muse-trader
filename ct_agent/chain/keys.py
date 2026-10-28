"""Solana addresses, keys and the program ids VIGIL relies on.

Addresses are base58 and case-sensitive: compare them as given and never lowercase them (a lowercased
mint is a different, usually invalid, address).
"""
from __future__ import annotations

import json
import re

import base58
from solders.keypair import Keypair

SOL_MINT = "So11111111111111111111111111111111111111112"      # wrapped SOL, the quote side of every swap
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SYSTEM_PROGRAM = "11111111111111111111111111111111"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"     # pump.fun bonding curves
PUMP_AMM_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"  # PumpSwap, where pump.fun coins graduate to
INCINERATOR = "1nc1nerator11111111111111111111111111111111"
MAINNET_GENESIS = "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d"

# A base58 run of address length, not touching other base58 characters (for pulling mints out of text)
BASE58_ADDRESS = re.compile(r"(?<![1-9A-HJ-NP-Za-km-z])[1-9A-HJ-NP-Za-km-z]{32,44}(?![1-9A-HJ-NP-Za-km-z])")


def is_pubkey(s: str) -> bool:
    """A valid Solana address: base58 that decodes to exactly 32 bytes."""
    if not isinstance(s, str) or not BASE58_ADDRESS.fullmatch(s):
        return False
    try:
        return len(base58.b58decode(s)) == 32
    except ValueError:
        return False


def find_pubkeys(text: str) -> list[str]:
    """Every valid address in a piece of text, in order, without duplicates."""
    out: list[str] = []
    for m in BASE58_ADDRESS.findall(text or ""):
        if m not in out and is_pubkey(m):
            out.append(m)
    return out


def load_keypair(secret: str) -> Keypair:
    """A wallet key as Phantom/Solflare export it (base58, 64 bytes) or as a solana-keygen JSON array."""
    s = (secret or "").strip()
    if not s:
        raise ValueError("no key")
    if s.startswith("["):
        return Keypair.from_bytes(bytes(json.loads(s)))
    raw = base58.b58decode(s)
    if len(raw) == 64:
        return Keypair.from_bytes(raw)
    if len(raw) == 32:
        return Keypair.from_seed(raw)
    raise ValueError("not a Solana private key (expected 64 bytes base58 or a JSON byte array)")


def short(addr: str) -> str:
    return f"{addr[:4]}…{addr[-4:]}" if addr and len(addr) > 10 else addr
