"""X API v2 client: budgeted reads, attention counts and original posts.

Policy (X automation rules + the brief):
  * official API only, no browser automation or scraping;
  * automated replies only to people who mention or reply to the account first, capped (social/replies.py);
  * the account must carry X's "Automated" label (set in account settings);
  * every read is paid, so reads go through a monthly budget. The budget is
    counted in post-read units ($0.005); an author profile returned with a
    post is a user read ($0.010), so it counts as 2 units.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from ..db import kv_get, kv_set
from .store import SourceIn, add_source

API = "https://api.x.com/2"
USER_READ_UNITS = 2  # a user read costs twice a post read


class XBudgetExceeded(RuntimeError):
    pass


class XNotConfigured(RuntimeError):
    pass


@dataclass
class PostResult:
    post_id: str
    text: str


class XClient:
    def __init__(self, *, bearer_token: str = "", user_token: str = "", monthly_read_budget: int = 20_000,
                 conn: sqlite3.Connection | None = None, transport: httpx.BaseTransport | None = None,
                 oauth: Any = None):
        self.bearer = bearer_token
        self.user_token = user_token
        self.oauth = oauth  # XOAuth2: refreshes the @MooseByMuse posting token automatically
        self.budget = monthly_read_budget
        self.conn = conn
        self._http = httpx.Client(timeout=20, transport=transport)

    # --- budget -------------------------------------------------------------

    def _budget_key(self) -> str:
        return "x_reads_" + datetime.now(timezone.utc).strftime("%Y-%m")

    def reads_used(self) -> int:
        return int(kv_get(self.conn, self._budget_key(), 0)) if self.conn else 0

    def _charge(self, n: int) -> None:
        if self.conn is not None:
            kv_set(self.conn, self._budget_key(), self.reads_used() + n)

    def _check_budget(self, n: int) -> None:
        if self.reads_used() + n > self.budget:
            raise XBudgetExceeded(f"monthly X read budget {self.budget} would be exceeded")

    # --- reads --------------------------------------------------------------

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self.bearer:
            raise XNotConfigured("CT_X_BEARER_TOKEN not set")
        r = self._http.get(f"{API}{path}", params=params, headers={"authorization": f"Bearer {self.bearer}"})
        r.raise_for_status()
        return r.json()

    def search_recent(self, query: str, max_results: int = 25) -> list[dict[str, Any]]:
        max_results = max(10, min(100, max_results))
        self._check_budget(max_results * (1 + USER_READ_UNITS))  # worst case: a new author per post
        body = self._get("/tweets/search/recent", {
            "query": query, "max_results": max_results,
            "tweet.fields": "created_at,author_id,public_metrics,referenced_tweets,entities",
            "expansions": "author_id", "user.fields": "username,verified,public_metrics",
        })
        posts = body.get("data") or []
        users = {u["id"]: u for u in (body.get("includes") or {}).get("users", [])}
        self._charge(len(posts) + USER_READ_UNITS * len(users))
        for p in posts:
            p["_author"] = users.get(p.get("author_id"), {})
        return posts

    def user_by_username(self, handle: str) -> dict[str, Any] | None:
        """Public profile of an X account (followers, age, bio). One user read."""
        self._check_budget(USER_READ_UNITS)
        body = self._get(f"/users/by/username/{handle}", {
            "user.fields": "created_at,description,public_metrics,verified,verified_type"})
        self._charge(USER_READ_UNITS)
        return body.get("data")

    def mention_counts(self, query: str) -> int:
        """Hourly post counts for the last 7 days, summed over the last 24h.
        Used to measure attention for the 'underfollowed' check."""
        body = self._get("/tweets/counts/recent", {"query": query, "granularity": "hour"})
        self._charge(1)
        buckets = body.get("data") or []
        return int(sum(b.get("tweet_count", 0) for b in buckets[-24:]))

    def ingest_search(self, conn: sqlite3.Connection, query: str, max_results: int = 25) -> int:
        created = 0
        for p in self.search_recent(query, max_results):
            user = p.get("_author", {})
            url = f"https://x.com/{user.get('username', 'i')}/status/{p['id']}"
            cites = None
            for u in (p.get("entities") or {}).get("urls", []) or []:
                cites = u.get("expanded_url") or cites
            for ref in p.get("referenced_tweets") or []:
                if ref.get("type") in ("quoted", "retweeted"):
                    cites = cites or f"https://x.com/i/status/{ref['id']}"
            _, new = add_source(conn, SourceIn(
                kind="x", origin_url=url, origin_id=p["id"], author=user.get("username"),
                published_at=p.get("created_at"), excerpt=p.get("text", ""), confidence="rumor",
                cites_url=cites, raw={"metrics": p.get("public_metrics"), "query": query},
            ))
            created += int(new)
        return created

    def mentions(self, user_id: str, since_id: str | None = None, max_results: int = 20) -> list[dict[str, Any]]:
        """Posts that mention the account, newest first. Post reads only (no author profiles), to keep it cheap."""
        max_results = max(5, min(100, max_results))
        self._check_budget(max_results)
        params: dict[str, Any] = {"max_results": max_results,
                                  "tweet.fields": "created_at,author_id,conversation_id,referenced_tweets"}
        if since_id:
            params["since_id"] = since_id
        body = self._get(f"/users/{user_id}/mentions", params)
        posts = body.get("data") or []
        self._charge(len(posts))
        return posts

    # --- writes -------------------------------------------------------------

    @property
    def can_post(self) -> bool:
        return bool(self.user_token) or bool(self.oauth and self.oauth.configured)

    def _user_token(self) -> str:
        if self.user_token:
            return self.user_token
        if self.oauth is not None:
            return self.oauth.access_token()  # raises XNotConfigured if the login is missing
        raise XNotConfigured("X posting not set up: run `ctagent x-login`")

    def my_user_id(self) -> str:
        """The posting account's own user id (from its login, so it's always the account we post as)."""
        r = self._http.get(f"{API}/users/me", headers={"authorization": f"Bearer {self._user_token()}"})
        r.raise_for_status()
        return str(r.json()["data"]["id"])

    def create_post(self, text: str, reply_to: str | None = None) -> PostResult:
        token = self._user_token()
        body: dict[str, Any] = {"text": text}
        if reply_to:  # threads under MOOSE's own posts, or an auto-reply to someone who engaged first
            body["reply"] = {"in_reply_to_tweet_id": reply_to}
        r = self._http.post(f"{API}/tweets", json=body,
                            headers={"authorization": f"Bearer {token}"})
        r.raise_for_status()
        data = r.json()["data"]
        return PostResult(post_id=str(data["id"]), text=data.get("text", text))
