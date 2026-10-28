"""Generic JSON-RPC client over httpx: retries, a backup node, batching and redacted URLs.

Chain-specific calls live on top of it (chain/solana.py). Every call goes through one method, so tests can
swap in an httpx.MockTransport and exercise the real encoding/decoding.
"""
from __future__ import annotations

import itertools
import logging
import re
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)


def redact(url: str) -> str:
    """Hide the API key in provider URLs (…/v2/<key>, ?api-key=<key>) before it reaches a log."""
    url = re.sub(r"(/v\d+/)[^/?#\s]+", r"\1***", url)
    return re.sub(r"((?:api[-_]?key|key|token)=)[^&#\s]+", r"\1***", url, flags=re.I)


class RpcError(RuntimeError):
    def __init__(self, message: str, code: int | None = None, data: Any = None):
        super().__init__(message)
        self.code = code
        self.data = data


class RpcClient:
    def __init__(self, url: str, *, timeout: float = 20.0, retries: int = 4,
                 transport: httpx.BaseTransport | None = None, backup_url: str | None = None):
        self.url = url
        # The backup node answers when the main provider is out of retries (e.g. a free-tier rate limit).
        self.backup_url = backup_url or url
        self.retries = retries
        self._ids = itertools.count(1)
        self._http = httpx.Client(timeout=timeout, transport=transport)

    def close(self) -> None:
        self._http.close()

    def request(self, method: str, params: Any, url: str | None = None) -> Any:
        target = url or self.url
        payload = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params}
        delay = 1.0
        for attempt in range(self.retries + 1):
            try:
                resp = self._http.post(target, json=payload)
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise httpx.HTTPStatusError("retryable", request=resp.request, response=resp)
                if resp.status_code >= 400:
                    # Providers report limits as 4xx with a JSON-RPC error body; surface the message.
                    try:
                        err = resp.json().get("error") or {}
                    except ValueError:
                        err = {}
                    raise RpcError(err.get("message") or f"{method}: HTTP {resp.status_code} from {redact(target)}",
                                   err.get("code"), err.get("data"))
                body = resp.json()
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                if attempt == self.retries:
                    status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else type(exc).__name__
                    if target != self.backup_url:
                        log.warning("%s: %s from %s, using backup node", method, status, redact(target))
                        return self.request(method, params, url=self.backup_url)
                    raise RpcError(f"{method} failed after retries ({status}) from {redact(target)}") from None
                time.sleep(delay)
                delay *= 2
                continue
            if "error" in body:
                err = body["error"]
                raise RpcError(err.get("message", "rpc error"), err.get("code"), err.get("data"))
            return body["result"]
        raise RpcError(f"{method}: unreachable")

    def _post_batch(self, payload: list[dict[str, Any]]) -> Any:
        targets = [self.url] + ([self.backup_url] if self.backup_url != self.url else [])
        for n, target in enumerate(targets):
            delay = 1.0
            for attempt in range(self.retries + 1):
                try:
                    resp = self._http.post(target, json=payload)
                    if resp.status_code == 429 or resp.status_code >= 500:
                        raise httpx.HTTPStatusError("retryable", request=resp.request, response=resp)
                    resp.raise_for_status()
                    return resp.json()
                except (httpx.TransportError, httpx.HTTPStatusError):
                    if attempt == self.retries:
                        break
                    time.sleep(delay)
                    delay *= 2
            if n + 1 < len(targets):
                log.warning("batch failed on %s, using backup node", redact(target))
        raise RpcError(f"batch failed after retries from {redact(targets[-1])}")

    def batch(self, calls: list[tuple[str, Any]], size: int = 20) -> list[Any]:
        """JSON-RPC batch. Returns results in order; an item that errored is
        returned as an RpcError instance instead of raising for the lot."""
        out: list[Any] = []
        for i in range(0, len(calls), size):
            chunk = calls[i:i + size]
            ids = [next(self._ids) for _ in chunk]
            payload = [{"jsonrpc": "2.0", "id": ids[j], "method": m, "params": p} for j, (m, p) in enumerate(chunk)]
            body = self._post_batch(payload)
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
