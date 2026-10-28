"""GMGN's trading API (openapi.gmgn.ai), an alternative venue to Jupiter (CT_LIVE_VENUE=gmgn, off by default):
swaps are submitted to GMGN on chain "sol", which signs them with the trading wallet bound to the API key, then
polled until they confirm onchain.

Every trade request is signed with the Ed25519 key whose public half is uploaded to GMGN: the message is
`{path}:{sorted query}:{body}:{timestamp}`, sent base64 in X-Signature. The timestamp must be within 5s of
GMGN's clock, and each client_id is single-use.
"""
from __future__ import annotations

import base64
import json
import time
import uuid
from typing import Any
from urllib.parse import quote

import httpx

from ..chain.keys import SOL_MINT

NATIVE = SOL_MINT  # SOL, as GMGN's Solana API names it (the wrapped-SOL mint)
DONE = {"confirmed", "failed", "expired"}


class GmgnError(RuntimeError):
    pass


def _qs(params: dict[str, Any]) -> str:
    return "&".join(f"{quote(k, safe='')}={quote(str(params[k]), safe='')}" for k in sorted(params))


class GmgnClient:
    def __init__(self, api_key: str, private_key_pem: str, host: str = "https://openapi.gmgn.ai",
                 transport: httpx.BaseTransport | None = None):
        from Crypto.PublicKey import ECC
        from Crypto.Signature import eddsa
        self.api_key = api_key
        self._signer = eddsa.new(ECC.import_key(private_key_pem.replace("\\n", "\n")), "rfc8032")
        self.host = host.rstrip("/")
        self._http = httpx.Client(timeout=20, transport=transport)

    def _request(self, method: str, path: str, query: dict[str, Any], body: dict[str, Any] | None,
                 signed: bool) -> dict[str, Any]:
        ts = int(time.time())
        q = {**query, "timestamp": ts, "client_id": str(uuid.uuid4())}
        raw = json.dumps(body, separators=(",", ":")) if body is not None else ""
        headers = {"X-APIKEY": self.api_key, "Content-Type": "application/json"}
        if signed:
            msg = f"{path}:{_qs(q)}:{raw}:{ts}".encode()
            headers["X-Signature"] = base64.b64encode(self._signer.sign(msg)).decode()
        r = self._http.request(method, f"{self.host}{path}?{_qs(q)}", headers=headers, content=raw or None)
        try:
            j = r.json()
        except ValueError:
            raise GmgnError(f"{path}: HTTP {r.status_code} {r.text[:200]}")
        if r.status_code != 200 or j.get("code") not in (0, "0"):
            raise GmgnError(f"{path}: HTTP {r.status_code} {j.get('error') or ''} {j.get('message') or ''}".strip())
        return j.get("data") or {}

    def swap(self, *, chain: str, wallet: str, input_token: str, output_token: str, amount: int,
             slippage_pct: float | None) -> dict[str, Any]:
        body: dict[str, Any] = {"chain": chain, "from_address": wallet, "input_token": input_token,
                                "output_token": output_token, "input_amount": str(amount)}
        if slippage_pct:
            body["slippage"] = slippage_pct
        else:
            body["auto_slippage"] = True
        return self._request("POST", "/v1/trade/swap", {}, body, signed=True)

    def order(self, chain: str, order_id: str) -> dict[str, Any]:
        return self._request("GET", "/v1/trade/query_order", {"order_id": order_id, "chain": chain}, None, signed=True)

    def wait(self, chain: str, order_id: str, timeout_s: float = 90, every_s: float = 1.5) -> dict[str, Any]:
        """Poll an order until it confirms, fails or expires. Raises GmgnError on timeout (state unknown)."""
        end = time.time() + timeout_s
        while True:
            o = self.order(chain, order_id)
            if o.get("status") in DONE:
                return o
            if time.time() > end:
                raise GmgnError(f"order {order_id} still {o.get('status')} after {timeout_s:.0f}s")
            time.sleep(every_s)
