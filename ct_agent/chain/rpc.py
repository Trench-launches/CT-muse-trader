"""Minimal EVM JSON-RPC client over httpx.

Kept small on purpose: every call goes through one method, so tests can
swap in an httpx.MockTransport and exercise the real encoding/decoding.
"""
from __future__ import annotations

import itertools
import logging
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)


class RpcError(RuntimeError):
    def __init__(self, message: str, code: int | None = None, data: Any = None):
        super().__init__(message)
        self.code = code
        self.data = data


class RpcClient:
    def __init__(self, url: str, *, timeout: float = 20.0, retries: int = 4,
                 transport: httpx.BaseTransport | None = None):
        self.url = url
        self.retries = retries
        self._ids = itertools.count(1)
        self._http = httpx.Client(timeout=timeout, transport=transport)

    def close(self) -> None:
        self._http.close()

    def request(self, method: str, params: list[Any]) -> Any:
        payload = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params}
        delay = 1.0
        for attempt in range(self.retries + 1):
            try:
                resp = self._http.post(self.url, json=payload)
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise httpx.HTTPStatusError("retryable", request=resp.request, response=resp)
                resp.raise_for_status()
                body = resp.json()
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                if attempt == self.retries:
                    raise RpcError(f"{method} failed after retries: {exc}") from exc
                time.sleep(delay)
                delay *= 2
                continue
            if "error" in body:
                err = body["error"]
                raise RpcError(err.get("message", "rpc error"), err.get("code"), err.get("data"))
            return body["result"]
        raise RpcError(f"{method}: unreachable")

    def batch(self, calls: list[tuple[str, list[Any]]], size: int = 20) -> list[Any]:
        """JSON-RPC batch. Returns results in order; an item that errored is
        returned as an RpcError instance instead of raising for the lot."""
        out: list[Any] = []
        for i in range(0, len(calls), size):
            chunk = calls[i:i + size]
            ids = [next(self._ids) for _ in chunk]
            payload = [{"jsonrpc": "2.0", "id": ids[j], "method": m, "params": p} for j, (m, p) in enumerate(chunk)]
            delay = 1.0
            for attempt in range(self.retries + 1):
                try:
                    resp = self._http.post(self.url, json=payload)
                    if resp.status_code == 429 or resp.status_code >= 500:
                        raise httpx.HTTPStatusError("retryable", request=resp.request, response=resp)
                    resp.raise_for_status()
                    body = resp.json()
                    break
                except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                    if attempt == self.retries:
                        raise RpcError(f"batch failed after retries: {exc}") from exc
                    time.sleep(delay)
                    delay *= 2
            if isinstance(body, dict):  # provider rejected batching: fall back
                for m, p in chunk:
                    try:
                        out.append(self.request(m, p))
                    except RpcError as exc:
                        out.append(exc)
                continue
            by_id = {item.get("id"): item for item in body}
            for j in ids:
                item = by_id.get(j, {"error": {"message": "missing from batch response"}})
                out.append(RpcError(item["error"].get("message", "rpc error")) if "error" in item else item["result"])
        return out

    # --- convenience --------------------------------------------------------

    def chain_id(self) -> int:
        return int(self.request("eth_chainId", []), 16)

    def block_number(self) -> int:
        return int(self.request("eth_blockNumber", []), 16)

    def block_timestamp(self, number: int) -> int:
        blk = self.request("eth_getBlockByNumber", [hex(number), False])
        return int(blk["timestamp"], 16)

    def get_code(self, address: str) -> str:
        return self.request("eth_getCode", [address, "latest"])

    def eth_call(self, to: str, data: str, block: str | int = "latest", sender: str | None = None,
                 value: int = 0) -> str:
        call: dict[str, Any] = {"to": to, "data": data}
        if sender:
            call["from"] = sender
        if value:
            call["value"] = hex(value)
        tag = hex(block) if isinstance(block, int) else block
        return self.request("eth_call", [call, tag])

    def get_balance(self, address: str) -> int:
        return int(self.request("eth_getBalance", [address, "latest"]), 16)

    def nonce(self, address: str) -> int:
        return int(self.request("eth_getTransactionCount", [address, "pending"]), 16)

    def gas_price(self) -> int:
        return int(self.request("eth_gasPrice", []), 16)

    def estimate_gas(self, tx: dict[str, Any]) -> int:
        return int(self.request("eth_estimateGas", [tx]), 16)

    def send_raw(self, raw_hex: str) -> str:
        return self.request("eth_sendRawTransaction", [raw_hex])

    def receipt(self, tx_hash: str) -> dict[str, Any] | None:
        return self.request("eth_getTransactionReceipt", [tx_hash])

    def get_logs(self, *, address: str | list[str] | None, topics: list[Any], from_block: int,
                 to_block: int) -> list[dict[str, Any]]:
        f: dict[str, Any] = {"topics": topics, "fromBlock": hex(from_block), "toBlock": hex(to_block)}
        if address:
            f["address"] = address
        return self.request("eth_getLogs", [f])

    def get_logs_chunked(self, *, address: str | list[str] | None, topics: list[Any], from_block: int,
                         to_block: int, chunk: int) -> list[dict[str, Any]]:
        """Split big ranges. Halves the chunk when a provider rejects a
        range as too large."""
        out: list[dict[str, Any]] = []
        start = from_block
        size = max(1, chunk)
        while start <= to_block:
            end = min(to_block, start + size - 1)
            try:
                out.extend(self.get_logs(address=address, topics=topics, from_block=start, to_block=end))
            except RpcError as exc:
                if size > 1 and _looks_like_range_error(exc):
                    size //= 2
                    log.info("getLogs range too large, shrinking chunk to %d", size)
                    continue
                raise
            start = end + 1
        return out


def _looks_like_range_error(exc: RpcError) -> bool:
    msg = str(exc).lower()
    return any(s in msg for s in ("range", "too many", "limit", "exceed", "10000", "block"))
