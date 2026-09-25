"""OAuth 2.0 (PKCE) user login for posting as @MooseByMuse, with automatic refresh.

X user access tokens last two hours. `ctagent x-login` runs the browser consent
once and yields a refresh token; the server keeps its current token pair in the
database and swaps the refresh token for a new pair whenever the access token
expires. X rotates refresh tokens on every use, so exactly one process (the
server) should hold it: the login writes it to .env as CT_X_REFRESH_TOKEN, it is
copied to the host's variables, and the server seeds its database from it.
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import secrets
import sqlite3
import threading
import time
import urllib.parse
import webbrowser
from typing import Any

import httpx

from ..db import journal_append, kv_get, kv_set
from .x_api import XNotConfigured

AUTH_URL = "https://x.com/i/oauth2/authorize"
TOKEN_URL = "https://api.x.com/2/oauth2/token"
ME_URL = "https://api.x.com/2/users/me"
SCOPES = "tweet.read tweet.write users.read media.write offline.access"
KV_KEY = "x_oauth2"
REDIRECT_URI = "http://127.0.0.1:8765/callback"


def _seed_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:16]


class XOAuth2:
    def __init__(self, *, client_id: str, client_secret: str = "", conn: sqlite3.Connection | None = None,
                 seed_refresh_token: str = "", transport: httpx.BaseTransport | None = None):
        self.client_id = client_id
        self.client_secret = client_secret
        self.conn = conn
        self.seed = seed_refresh_token
        self._http = httpx.Client(timeout=20, transport=transport)

    @property
    def configured(self) -> bool:
        return bool(self.client_id and (self.seed or (self.conn is not None and kv_get(self.conn, KV_KEY))))

    def _auth(self) -> tuple[str, str] | None:
        # Confidential clients ("Web App, Automated App or Bot") authenticate with Basic auth.
        return (self.client_id, self.client_secret) if self.client_secret else None

    def _state(self) -> dict[str, Any] | None:
        st = kv_get(self.conn, KV_KEY) if self.conn is not None else None
        # A new login (a different CT_X_REFRESH_TOKEN) replaces whatever the server held.
        if self.seed and (not st or st.get("seed_hash") != _seed_hash(self.seed)):
            st = {"refresh_token": self.seed, "access_token": "", "expires_at": 0, "seed_hash": _seed_hash(self.seed)}
        return st

    def _save(self, tok: dict[str, Any], seed_hash: str | None) -> dict[str, Any]:
        st = {"access_token": tok["access_token"], "refresh_token": tok.get("refresh_token", ""),
              "expires_at": time.time() + int(tok.get("expires_in", 7200)) - 120, "seed_hash": seed_hash}
        if self.conn is not None:
            kv_set(self.conn, KV_KEY, st)
        return st

    def access_token(self) -> str:
        st = self._state()
        if not self.client_id or not st or not st.get("refresh_token"):
            raise XNotConfigured("X posting login missing: run `ctagent x-login` and set CT_X_REFRESH_TOKEN")
        if st.get("access_token") and st.get("expires_at", 0) > time.time():
            return st["access_token"]
        r = self._http.post(TOKEN_URL, auth=self._auth(), data={
            "grant_type": "refresh_token", "refresh_token": st["refresh_token"], "client_id": self.client_id})
        if r.status_code >= 400:
            if self.conn is not None:
                journal_append(self.conn, "x_auth_failed", {"status": r.status_code, "body": r.text[:200]})
            raise XNotConfigured(f"X token refresh failed ({r.status_code}); run `ctagent x-login` again")
        return self._save(r.json(), st.get("seed_hash"))["access_token"]

    # --- one-time browser login ------------------------------------------------

    def exchange_code(self, code: str, verifier: str, redirect_uri: str = REDIRECT_URI) -> dict[str, Any]:
        r = self._http.post(TOKEN_URL, auth=self._auth(), data={
            "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
            "code_verifier": verifier, "client_id": self.client_id})
        if r.status_code >= 400:
            raise RuntimeError(f"X rejected the login ({r.status_code}): {r.text[:300]}")
        return r.json()

    def whoami(self, access_token: str) -> str:
        r = self._http.get(ME_URL, headers={"authorization": f"Bearer {access_token}"})
        r.raise_for_status()
        return r.json()["data"]["username"]


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def authorize_url(client_id: str, challenge: str, state: str, redirect_uri: str = REDIRECT_URI) -> str:
    return AUTH_URL + "?" + urllib.parse.urlencode({
        "response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri, "scope": SCOPES,
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})


def browser_login(oauth: XOAuth2, *, open_browser: bool = True, timeout_s: int = 900) -> tuple[str, str]:
    """Run the consent flow on 127.0.0.1:8765. Returns (username, refresh_token)."""
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(24)
    result: dict[str, str] = {}
    done = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            ok = q.get("state", [""])[0] == state and "code" in q
            if ok:
                result["code"] = q["code"][0]
            else:
                result["error"] = q.get("error_description", q.get("error", ["login was not completed"]))[0]
            body = ("<h2>MOOSE is connected to X.</h2><p>You can close this tab.</p>" if ok
                    else f"<h2>Login failed</h2><p>{result['error']}</p>").encode()
            self.send_response(200 if ok else 400)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)
            done.set()

        def log_message(self, *_: object) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 8765), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = authorize_url(oauth.client_id, challenge, state)
    print("Open this link and approve as @MooseByMuse (it should open automatically):\n" + url, flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        if not done.wait(timeout_s):
            raise TimeoutError("no response from X within 15 minutes")
    finally:
        server.shutdown()
    if "code" not in result:
        raise RuntimeError(result.get("error", "login failed"))
    tok = oauth.exchange_code(result["code"], verifier)
    if not tok.get("refresh_token"):
        raise RuntimeError("X did not return a refresh token; check the app requests offline.access")
    return oauth.whoami(tok["access_token"]), tok["refresh_token"]
