"""A coin's own story: the description and social links its creator published
with the coin (its Metaplex metadata JSON, e.g. what pump.fun stores on IPFS, with
DexScreener's token info as a fallback), plus a quick look at those links: the X
account's profile and the website's title and summary.

All of it is written by the coin's creator, so it is a claim, not proof. It is
handed to Grok as untrusted text, next to the third-party evidence.

The metadata and website fetches only go to public internet addresses (never this
server's own network), with a short timeout, a size cap and no automatic redirects.
"""
from __future__ import annotations

import ipaddress
import logging
import re
import socket
import time
from html import unescape
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from ..db import kv_get, kv_set

log = logging.getLogger(__name__)

URL = re.compile(r"https?://[^\s,;\"'<>]+", re.I)
X_HOSTS = {"x.com", "twitter.com", "www.x.com", "www.twitter.com", "mobile.twitter.com", "mobile.x.com"}
NOT_A_PROFILE = {"i", "intent", "home", "search", "share", "hashtag", "explore"}
TTL_S = 12 * 3600
MAX_PAGE_BYTES = 200_000


def split_links(socials: str) -> dict[str, Any]:
    """{'x': handle or None, 'website': url or None, 'other': [urls]} from a socials string."""
    out: dict[str, Any] = {"x": None, "website": None, "other": []}
    for url in URL.findall(socials or ""):
        url = url.rstrip(").]")
        host = (urlparse(url).hostname or "").lower()
        if host in X_HOSTS:
            seg = urlparse(url).path.strip("/").split("/")[0].lstrip("@")
            if not out["x"] and re.fullmatch(r"[A-Za-z0-9_]{1,15}", seg or "") and seg.lower() not in NOT_A_PROFILE:
                out["x"] = seg
        elif not out["website"] and not any(h in host for h in ("t.me", "telegram", "discord", "linktr.ee")):
            out["website"] = url
        else:
            out["other"].append(url)
    return out


def _public_host(host: str) -> bool:
    """True only if every address the host resolves to is on the public internet."""
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError, OSError):
        return False
    addrs = {info[4][0] for info in infos}
    return bool(addrs) and all(ipaddress.ip_address(a.split("%")[0]).is_global for a in addrs)


def _text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", html)
    return " ".join(unescape(re.sub(r"(?s)<[^>]+>", " ", html)).split())


def _meta(html: str, *names: str) -> str:
    for n in names:
        m = re.search(rf'(?is)<meta[^>]+(?:name|property)=["\']{re.escape(n)}["\'][^>]*content=["\']([^"\']*)', html) \
            or re.search(rf'(?is)<meta[^>]+content=["\']([^"\']*)["\'][^>]*(?:name|property)=["\']{re.escape(n)}["\']', html)
        if m and m.group(1).strip():
            return unescape(m.group(1).strip())
    return ""


def site_summary(http: httpx.Client, url: str) -> dict[str, Any] | None:
    """Title, meta description and the first bit of text of a public website. None if it can't be read."""
    for _ in range(3):  # follow up to two redirects, checking each hop
        p = urlparse(url)
        if p.scheme not in ("http", "https") or not p.hostname or not _public_host(p.hostname):
            return None
        try:
            with http.stream("GET", url, follow_redirects=False, timeout=6,
                             headers={"user-agent": "ct-agent research bot (+https://github.com/Trench-launches/vigil)"}) as r:
                if r.is_redirect and r.headers.get("location"):
                    url = urljoin(url, r.headers["location"])
                    continue
                if r.status_code >= 400 or "html" not in r.headers.get("content-type", "html"):
                    return None
                body = b""
                for chunk in r.iter_bytes():
                    body += chunk
                    if len(body) >= MAX_PAGE_BYTES:
                        break
        except httpx.HTTPError:
            return None
        html = body.decode("utf-8", "replace")
        title = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
        return {"url": url, "title": _text(title.group(1))[:120] if title else "",
                "description": _meta(html, "description", "og:description", "twitter:description")[:300],
                "text": _text(html)[:500]}
    return None


def x_profile(x: Any, handle: str) -> dict[str, Any] | None:
    try:
        u = x.user_by_username(handle)
    except Exception as exc:  # no token, budget spent, account gone: the lore just goes without it
        log.info("x profile for @%s unavailable: %s", handle, exc)
        return None
    if not u:
        return None
    m = u.get("public_metrics") or {}
    return {"handle": u.get("username") or handle, "name": u.get("name"), "bio": (u.get("description") or "")[:300],
            "followers": m.get("followers_count"), "posts": m.get("tweet_count"), "created_at": u.get("created_at"),
            "verified": bool(u.get("verified")) or u.get("verified_type") not in (None, "", "none")}


def cached(ctx: Any, token: str) -> tuple[dict[str, Any] | None, bool]:
    """(lore, looked) from the 12h cache, or (None, False)."""
    hit = kv_get(ctx.conn, f"lore:{token}")
    if hit and time.time() - hit.get("at", 0) < TTL_S:
        return hit["lore"], bool(hit.get("looked"))
    return None, False


def save(ctx: Any, token: str, lore: dict[str, Any], looked: bool) -> None:
    kv_set(ctx.conn, f"lore:{token}", {"at": time.time(), "looked": looked, "lore": lore})


class _PublicOnly:
    """An httpx client that only fetches from public internet hosts: the metadata link comes from the coin's
    creator, so it must never reach this server's own network."""

    def __init__(self, http: httpx.Client):
        self._http = http

    def get(self, url: str, **kw: Any) -> httpx.Response:
        host = urlparse(url).hostname
        if urlparse(url).scheme not in ("http", "https") or not host or not _public_host(host):
            raise httpx.HTTPError(f"not a public host: {host}")
        return self._http.get(url, follow_redirects=False, **kw)


def _dexscreener_info(ctx: Any, token: str) -> dict[str, Any]:
    """DexScreener's token info (links the team registered there), from the coin's deepest pair."""
    market = getattr(ctx, "market", None)
    if market is None:
        return {}
    try:
        pairs = market.pairs(token)
    except Exception as exc:  # the lore just goes without it
        log.info("dexscreener info for %s unavailable: %s", token, exc)
        return {}
    infos = [p.get("info") for p in sorted(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0),
                                            reverse=True) if p.get("info")]
    return infos[0] if infos else {}


def read_onchain(ctx: Any, token: str) -> dict[str, Any]:
    """Just what the coin itself says: its description and links."""
    from ..chain.token import token_meta
    web = getattr(ctx, "web", None)
    try:
        meta = token_meta(ctx.rpc, token, http=_PublicOnly(web) if web is not None else None)
    except Exception as exc:  # no metadata: the lore is just thinner
        log.info("metadata for %s unavailable: %s", token, exc)
        meta = {}
    urls = [meta.get(k, "") for k in ("twitter", "website", "telegram")]
    if not any(urls) or not meta.get("description"):
        info = _dexscreener_info(ctx, token)
        urls += [w.get("url", "") for w in info.get("websites") or [] if isinstance(w, dict)]
        urls += [x.get("url", "") for x in info.get("socials") or [] if isinstance(x, dict)]
        meta.setdefault("description", str(info.get("description") or ""))
    links = split_links(" ".join(u for u in urls if u))
    return {"description": str(meta.get("description") or "")[:600], "links": {k: v for k, v in links.items() if v}}


def get_lore(ctx: Any, token: str, *, look: bool = True) -> dict[str, Any]:
    """The coin's lore, cached for 12h. `look` also visits the X account and website."""
    hit, looked = cached(ctx, token)
    if hit is not None and (looked or not look):
        return hit
    lore = read_onchain(ctx, token)
    if look:
        links = lore["links"]
        if links.get("x") and getattr(ctx, "x", None) is not None:
            lore["x_account"] = x_profile(ctx.x, links["x"])
        web = getattr(ctx, "web", None)
        if links.get("website") and web is not None:
            lore["website"] = site_summary(web, links["website"])
    save(ctx, token, lore, look)
    return lore
