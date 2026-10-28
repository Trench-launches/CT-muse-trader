"""Coin logos for the website.

Each coin's metadata names an image (pump.fun coins: an IPFS link), saved in the
token cache. The site never loads those links directly: the API fetches a logo once, checks
it really is an image, re-encodes it as a small 128px WebP (which also drops
anything else embedded in the file) and serves that from disk. Only tokens
VIGIL already tracks can be fetched, so this isn't an open image proxy.
"""
from __future__ import annotations

import io
import logging
import time
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

GATEWAYS = ["https://ipfs.io/ipfs/", "https://gateway.pinata.cloud/ipfs/", "https://dweb.link/ipfs/"]
# Image hosts fetched as given (fixed, well-known CDNs, never a host the coin's creator picks)
TRUSTED_HOSTS = {"dd.dexscreener.com", "cdn.dexscreener.com"}
MAX_BYTES = 5_000_000
SIZE = 128
RETRY_AFTER_S = 6 * 3600  # a logo that failed isn't retried for a while
_failed: dict[str, float] = {}


def _candidates(url: str) -> list[str]:
    """Gateway URLs for an IPFS logo. The link comes from whoever launched the
    token, so it is never fetched as given: only its IPFS content id is used,
    through our fixed public gateways. Other links are skipped, except images on
    DexScreener's own CDN."""
    from urllib.parse import urlparse
    url = url.strip()
    u = urlparse(url)
    if u.scheme == "https" and u.hostname in TRUSTED_HOSTS and not u.username and u.port is None:
        return [url]
    if url.startswith("ipfs://"):
        path = url[len("ipfs://"):].removeprefix("ipfs/")
    elif url.startswith("https://") and "/ipfs/" in url:
        path = url.split("/ipfs/", 1)[1]
    else:
        return []
    path = path.split("?", 1)[0].split("#", 1)[0]
    if not path or any(c in path for c in (" ", "\\", "@")) or ".." in path:
        return []
    return [g + path for g in GATEWAYS]


def _download(url: str, client: httpx.Client) -> bytes | None:
    try:
        with client.stream("GET", url, follow_redirects=True) as r:
            if r.status_code != 200:
                return None
            buf = bytearray()
            for chunk in r.iter_bytes():
                buf += chunk
                if len(buf) > MAX_BYTES:
                    return None
            return bytes(buf)
    except httpx.HTTPError:
        return None


def _to_webp(raw: bytes) -> bytes | None:
    """Decode as a raster image (PNG/JPEG/GIF/WebP only) and re-encode small."""
    from PIL import Image
    try:
        im = Image.open(io.BytesIO(raw))
        if im.format not in ("PNG", "JPEG", "GIF", "WEBP"):
            return None
        im.seek(0)
        im = im.convert("RGBA")
        im.thumbnail((SIZE, SIZE))
        out = io.BytesIO()
        im.save(out, "WEBP", quality=85)
        return out.getvalue()
    except Exception:
        return None


def logo_file(conn: Any, data_dir: Path, token: str, client: httpx.Client | None = None) -> Path | None:
    """Path to the cached WebP for a tracked token, fetching it on first use."""
    path = data_dir / "logos" / f"{token}.webp"  # base58 mint, exactly as given
    if path.exists():
        return path
    if time.time() - _failed.get(token, 0) < RETRY_AFTER_S:
        return None
    row = conn.execute("SELECT logo_url FROM tokens WHERE mint=?", (token,)).fetchone()
    urls = _candidates(row["logo_url"]) if row and row["logo_url"] else []
    own = client is None
    client = client or httpx.Client(timeout=8, headers={"user-agent": "VIGIL-logo-fetch"})
    try:
        for url in urls:
            raw = _download(url, client)
            img = _to_webp(raw) if raw else None
            if img:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".tmp")
                tmp.write_bytes(img)
                tmp.replace(path)
                return path
    finally:
        if own:
            client.close()
    _failed[token] = time.time()
    return None
