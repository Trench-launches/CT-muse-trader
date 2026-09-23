"""Pull candidate contract addresses out of chat, posts and transcripts.

Everything returned here is a *lead*. Nothing links a narrative to a token
until the address is verified onchain (see sources.store.verify_leads).
"""
from __future__ import annotations

import re

_EVM = re.compile(r"(?<![0-9a-fA-Fx])0x[0-9a-fA-F]{40}(?![0-9a-fA-F])")

# Speech-to-text often spells addresses out: "zero x a one b ..."
_SPOKEN = {
    "zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "a": "a", "b": "b", "c": "c", "d": "d", "e": "e", "f": "f",
    "ay": "a", "bee": "b", "see": "c", "sea": "c", "dee": "d", "ee": "e", "eff": "f",
}
_CHAIN_HINTS = {
    "robinhood": "robinhood", "rh chain": "robinhood", "pons": "robinhood",
    "base": "base", "ethereum": "ethereum", "mainnet": "ethereum", "arbitrum": "arbitrum",
    "solana": "solana", "sol ": "solana", "bsc": "bsc", "bnb": "bsc",
}


def extract_evm_addresses(text: str) -> list[str]:
    seen: dict[str, None] = {}
    for m in _EVM.findall(text or ""):
        seen.setdefault(m.lower(), None)
    for m in _spoken_addresses(text or ""):
        seen.setdefault(m, None)
    return list(seen)


def _spoken_addresses(text: str) -> list[str]:
    words = re.findall(r"[a-zA-Z0-9]+", text.lower())
    out = []
    for i, w in enumerate(words):
        if w not in ("0x", "zerox") and not (w in ("zero", "oh") and i + 1 < len(words) and words[i + 1] in ("x", "ex")):
            continue
        j = i + (1 if w in ("0x", "zerox") else 2)
        hexchars = []
        while j < len(words) and len(hexchars) < 40:
            tok = words[j]
            if tok in _SPOKEN:
                hexchars.append(_SPOKEN[tok])
            elif re.fullmatch(r"[0-9a-f]+", tok):
                hexchars.extend(tok)
            else:
                break
            j += 1
        if len(hexchars) == 40:
            out.append("0x" + "".join(hexchars))
    return out


def chain_hint(text: str) -> str | None:
    t = f" {(text or '').lower()} "
    for needle, chain in _CHAIN_HINTS.items():
        if needle in t:
            return chain
    return None
