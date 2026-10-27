"""Coin logos for the website.

Each Pons token stores an image link onchain (logo(), usually ipfs://). The
site never loads those links directly: the API fetches a logo once, checks
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
MAX_BYTES = 5_000_000
SIZE = 128
RETRY_AFTER_S = 6 * 3600  # a logo that failed isn't retried for a while
_failed: dict[str, float] = {}


def _candidates(url: str) -> list[str]:
    """Gateway URLs for an IPFS logo. The link comes from whoever launched the
    token, so it is never fetched as given: only its IPFS content id is used,
    through our fixed public gateways. Non-IPFS links are skipped."""
    url = url.strip()
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
    token = token.lower()
    path = data_dir / "logos" / f"{token}.webp"
    if path.exists():
        return path
    if time.time() - _failed.get(token, 0) < RETRY_AFTER_S:
        return None
    row = conn.execute("SELECT logo_url FROM launches WHERE lower(token)=?", (token,)).fetchone()
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
