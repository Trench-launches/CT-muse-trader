"""Twitch EventSub (webhook transport) and transcript intake.

  * stream.online tells us a watched channel went live.
  * channel.chat.message delivers chat, once the bot account has authorized
    the app (user:read:chat, user:bot) and the broadcaster has granted
    channel:bot or made the bot a moderator.
  * Chat is stored as 'sentiment', never as evidence that news is true.
  * VOD/clip transcription needs its own authorized path and creator rights,
    so transcripts enter through ingest_transcript() from whatever approved
    pipeline produces them, carrying timestamp, channel, link and confidence.
"""
from __future__ import annotations

import hashlib
import hmac
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from ..db import journal_append, kv_get, kv_set
from .store import SourceIn, add_source

HELIX = "https://api.twitch.tv/helix"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"
MAX_MESSAGE_AGE = timedelta(minutes=10)


class TwitchSignatureError(ValueError):
    pass


def parse_twitch_ts(ts: str) -> datetime:
    """Twitch sends RFC3339 with up to nanosecond precision, e.g.
    2026-09-23T10:11:12.634234626Z. Python accepts at most microseconds."""
    ts = ts.strip().removesuffix("Z")
    main, _, frac = ts.partition(".")
    iso = f"{main}.{frac[:6].ljust(6, '0')}" if frac else main
    return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)


def verify_signature(secret: str, headers: dict[str, str], body: bytes,
                     now: datetime | None = None) -> None:
    h = {k.lower(): v for k, v in headers.items()}
    msg_id = h.get("twitch-eventsub-message-id", "")
    ts = h.get("twitch-eventsub-message-timestamp", "")
    sig = h.get("twitch-eventsub-message-signature", "")
    if not (secret and msg_id and ts and sig):
        raise TwitchSignatureError("missing signature headers or secret")
    expected = "sha256=" + hmac.new(secret.encode(), (msg_id + ts).encode() + body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        raise TwitchSignatureError("bad signature")
    if (now or datetime.now(timezone.utc)) - parse_twitch_ts(ts) > MAX_MESSAGE_AGE:
        raise TwitchSignatureError("message too old (possible replay)")


def handle_eventsub(conn: sqlite3.Connection, secret: str, headers: dict[str, str], body: bytes,
                    payload: dict[str, Any]) -> tuple[int, str]:
    """Returns (http_status, response_text)."""
    verify_signature(secret, headers, body)
    h = {k.lower(): v for k, v in headers.items()}
    msg_id = h["twitch-eventsub-message-id"]
    mtype = h.get("twitch-eventsub-message-type")
    seen_key = f"twitch_msg:{msg_id}"
    if kv_get(conn, seen_key):
        return 204, ""  # duplicate delivery
    kv_set(conn, seen_key, 1)

    if mtype == "webhook_callback_verification":
        return 200, payload["challenge"]
    if mtype == "revocation":
        journal_append(conn, "twitch_revoked", {"subscription": payload.get("subscription")})
        return 204, ""

    sub_type = (payload.get("subscription") or {}).get("type")
    ev = payload.get("event") or {}
    if sub_type == "stream.online":
        login = ev.get("broadcaster_user_login")
        journal_append(conn, "twitch_live", {"channel": login, "started_at": ev.get("started_at"),
                                             "stream_id": ev.get("id")}, ref=login)
    elif sub_type == "channel.chat.message":
        login = ev.get("broadcaster_user_login")
        text = (ev.get("message") or {}).get("text", "")
        add_source(conn, SourceIn(
            kind="twitch_chat", origin_url=f"https://www.twitch.tv/{login}", origin_id=ev.get("message_id"),
            author=ev.get("chatter_user_login"), channel=login,
            published_at=h.get("twitch-eventsub-message-timestamp"), excerpt=text, confidence="sentiment",
        ))
    return 204, ""


def ingest_transcript(conn: sqlite3.Connection, *, channel: str, url: str, offset_s: int | None,
                      excerpt: str, confidence: str = "rumor", spoken_at: str | None = None) -> int:
    """Store a transcript excerpt from an authorized pipeline. Addresses in
    it become unverified leads automatically."""
    link = f"{url}?t={offset_s}s" if offset_s is not None else url
    sid, _ = add_source(conn, SourceIn(
        kind="twitch_transcript", origin_url=link, origin_id=f"{url}#{offset_s}", channel=channel,
        author=channel, published_at=spoken_at, excerpt=excerpt, confidence=confidence, cites_url=link,
    ))
    return sid


class TwitchApi:
    """Helix helper to create EventSub webhook subscriptions."""

    def __init__(self, client_id: str, client_secret: str, transport: httpx.BaseTransport | None = None):
        self.client_id = client_id
        self.client_secret = client_secret
        self._http = httpx.Client(timeout=20, transport=transport)
        self._token: str | None = None

    def app_token(self) -> str:
        if not self._token:
            r = self._http.post(TOKEN_URL, data={"client_id": self.client_id, "client_secret": self.client_secret,
                                                 "grant_type": "client_credentials"})
            r.raise_for_status()
            self._token = r.json()["access_token"]
        return self._token

    def _headers(self) -> dict[str, str]:
        return {"client-id": self.client_id, "authorization": f"Bearer {self.app_token()}"}

    def user_id(self, login: str) -> str:
        r = self._http.get(f"{HELIX}/users", params={"login": login}, headers=self._headers())
        r.raise_for_status()
        return r.json()["data"][0]["id"]

    def subscribe(self, sub_type: str, condition: dict[str, str], callback: str, secret: str) -> dict[str, Any]:
        r = self._http.post(f"{HELIX}/eventsub/subscriptions", headers=self._headers(), json={
            "type": sub_type, "version": "1", "condition": condition,
            "transport": {"method": "webhook", "callback": callback, "secret": secret},
        })
        r.raise_for_status()
        return r.json()

    def subscribe_channel(self, login: str, bot_user_id: str | None, callback: str, secret: str) -> list[str]:
        bid = self.user_id(login)
        done = [self.subscribe("stream.online", {"broadcaster_user_id": bid}, callback, secret)["data"][0]["id"]]
        if bot_user_id:
            done.append(self.subscribe("channel.chat.message",
                                       {"broadcaster_user_id": bid, "user_id": bot_user_id},
                                       callback, secret)["data"][0]["id"])
        return done
