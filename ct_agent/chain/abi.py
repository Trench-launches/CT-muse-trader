"""ABI helpers: selectors, call encoding, output and log decoding."""
from __future__ import annotations

from typing import Any, Sequence

from eth_abi import decode, encode
from eth_utils import keccak, to_checksum_address

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


def selector(signature: str) -> str:
    return "0x" + keccak(text=signature)[:4].hex()


def topic(signature: str) -> str:
    return "0x" + keccak(text=signature).hex()


def _arg_types(signature: str) -> list[str]:
    inner = signature[signature.index("(") + 1: signature.rindex(")")]
    return [t for t in inner.split(",") if t] if inner else []


def encode_call(signature: str, *args: Any) -> str:
    types = _arg_types(signature)
    data = encode(types, list(args)) if types else b""
    return selector(signature) + data.hex()


def decode_output(types: Sequence[str], hexdata: str) -> tuple[Any, ...]:
    raw = bytes.fromhex(hexdata[2:] if hexdata.startswith("0x") else hexdata)
    if not raw:
        raise ValueError("empty return data (call reverted or contract missing)")
    return decode(list(types), raw)


def topic_to_address(t: str) -> str:
    return to_checksum_address("0x" + t[-40:])


def address_topic(addr: str) -> str:
    return "0x" + "0" * 24 + addr.lower().removeprefix("0x")


def checksum(addr: str) -> str:
    return to_checksum_address(addr)
