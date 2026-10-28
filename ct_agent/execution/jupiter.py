"""Jupiter: the swap router VIGIL trades through, and its price feed.

Swap V2 `order` -> sign -> `execute` (https://developers.jup.ag/docs/swap/order-and-execute). Jupiter builds the
transaction and lands it; VIGIL only signs. RFQ routes (JupiterZ) are excluded so our signature is the only one
needed. Prices come from Price API v3, up to 50 mints a call. The free API key allows ~1 request/s, so calls
are paced and a 429 is retried after a pause.
"""
from __future__ import annotations

import base64
import threading
import time
from typing import Any

import httpx
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction


class JupiterError(RuntimeError):
    pass


class Jupiter:
    def __init__(self, api_url: str = "https://api.jup.ag", api_key: str = "", *, min_interval_s: float = 1.1,
                 timeout: float = 20.0, transport: httpx.BaseTransport | None = None):
        self.api_url = api_url.rstrip("/")
        self.min_interval_s = min_interval_s if api_key else max(min_interval_s, 2.1)  # keyless: 0.5 rps
        headers = {"x-api-key": api_key} if api_key else {}
        self._http = httpx.Client(timeout=timeout, transport=transport, headers=headers)
        self._lock = threading.Lock()
        self._last = 0.0

    def _call(self, method: str, path: str, **kw: Any) -> Any:
        for attempt in range(4):
            with self._lock:
                wait = self._last + self.min_interval_s - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                self._last = time.monotonic()
            try:
                r = self._http.request(method, self.api_url + path, **kw)
            except httpx.TransportError as exc:
                if attempt == 3:
                    raise JupiterError(f"{path}: {type(exc).__name__}") from None
                time.sleep(1 + attempt)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                if attempt == 3:
                    raise JupiterError(f"{path}: HTTP {r.status_code}")
                time.sleep(float(r.headers.get("retry-after") or 1 + attempt))
                continue
            try:
                body = r.json()
            except ValueError:
                raise JupiterError(f"{path}: HTTP {r.status_code}, not JSON") from None
            if r.status_code >= 400:
                raise JupiterError(f"{path}: {body.get('error') or body.get('errorMessage') or r.status_code}")
            return body
        raise JupiterError(f"{path}: unreachable")

    def order(self, input_mint: str, output_mint: str, amount: int, *, taker: str | None = None,
              slippage_bps: int | None = None) -> dict[str, Any]:
        """A quote, and with `taker` an unsigned transaction to swap `amount` base units of input for output."""
        params: dict[str, Any] = {"inputMint": input_mint, "outputMint": output_mint, "amount": str(int(amount)),
                                  "excludeRouters": "jupiterz"}
        if taker:
            params["taker"] = taker
        if slippage_bps is not None:
            params["slippageBps"] = int(slippage_bps)
        body = self._call("GET", "/swap/v2/order", params=params)
        if body.get("errorCode") or body.get("errorMessage") or not body.get("outAmount"):
            raise JupiterError(f"no route: {body.get('errorMessage') or body.get('errorCode') or 'no outAmount'}")
        return body

    def execute(self, signed_tx_b64: str, request_id: str) -> dict[str, Any]:
        body = self._call("POST", "/swap/v2/execute", json={"signedTransaction": signed_tx_b64, "requestId": request_id})
        if body.get("status") != "Success" or int(body.get("code", 0) or 0) != 0:
            raise JupiterError(f"swap failed: {body.get('error') or body.get('status')} (code {body.get('code')})")
        return body

    def prices(self, mints: list[str]) -> dict[str, dict[str, Any]]:
        """{mint: {usdPrice, decimals, liquidity, ...}} for the mints Jupiter can price (others are left out)."""
        out: dict[str, dict[str, Any]] = {}
        uniq = list(dict.fromkeys(m for m in mints if m))
        for i in range(0, len(uniq), 50):
            body = self._call("GET", "/price/v3", params={"ids": ",".join(uniq[i:i + 50])})
            out.update({k: v for k, v in (body or {}).items() if isinstance(v, dict) and v.get("usdPrice")})
        return out


def sign(unsigned_b64: str, keypair: Keypair) -> str:
    """Sign a Jupiter-built transaction (we are its only signer) and return it base64-encoded."""
    tx = VersionedTransaction.from_bytes(base64.b64decode(unsigned_b64))
    signed = VersionedTransaction(tx.message, [keypair])
    return base64.b64encode(bytes(signed)).decode()
