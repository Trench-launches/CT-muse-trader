"""Pull candidate Solana mint addresses out of chat, posts and transcripts.

Everything returned here is a *lead*. Nothing links a narrative to a token until the address is verified onchain
(see sources.store.verify_leads). Addresses are base58 and case-sensitive, so they are kept exactly as written.
"""
from __future__ import annotations

import re

from ..chain.keys import find_pubkeys

# Whole words that say which chain a post is about (checked in this order: Solana words win).
_CHAIN_HINTS = [
    (r"solana|sol|pump\.?fun|pumpswap|raydium|jupiter|solscan|meteora|phantom", "solana"),
    (r"robinhood|rh chain|pons", "robinhood"),
    (r"base chain|on base|basescan", "base"),
    (r"ethereum|etherscan|uniswap", "ethereum"),
    (r"arbitrum", "arbitrum"),
    (r"bsc|bnb|binance smart chain|pancakeswap", "bsc"),
    (r"tron|sui", "other"),
]
_HINTS = [(re.compile(rf"\b(?:{p})\b", re.I), chain) for p, chain in _CHAIN_HINTS]


def extract_addresses(text: str) -> list[str]:
    """Every valid Solana address in the text, in order, without duplicates."""
    return find_pubkeys(text or "")


def chain_hint(text: str) -> str | None:
    for rx, chain in _HINTS:
        if rx.search(text or ""):
            return chain
    return None
