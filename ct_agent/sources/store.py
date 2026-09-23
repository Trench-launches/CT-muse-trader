"""Evidence store: attributed sources, claim grouping and lead verification.

Independence rule from the brief: a claim repeated by five traders is still
one unverified claim if they all cite the same rumor. Each source records
what it cites (cites_url). Sources that share a root origin count once.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from ..chain.pons import PonsV2
from ..db import journal_append, rows, utcnow
from .addresses import chain_hint, extract_evm_addresses

CONFIDENCE = ("verified", "first_party", "reported", "rumor", "sentiment")


@dataclass
class SourceIn:
    kind: str
    excerpt: str
    confidence: str
    origin_url: str | None = None
    origin_id: str | None = None
    author: str | None = None
    channel: str | None = None
    published_at: str | None = None
    claim_key: str | None = None
    cites_url: str | None = None
    raw: dict[str, Any] | None = None


def normalize_url(url: str | None) -> str | None:
    if not url:
        return None
    parts = urlsplit(url.strip())
    host = parts.netloc.lower().removeprefix("www.")
    host = {"twitter.com": "x.com", "mobile.twitter.com": "x.com"}.get(host, host)
    return f"{host}{parts.path.rstrip('/')}".lower()


def claim_key_for(text: str) -> str:
    """Fallback grouping when no explicit claim key: normalized text hash."""
    t = re.sub(r"https?://\S+", "", text.lower())
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    t = " ".join(t.split())[:280]
    return hashlib.sha1(t.encode()).hexdigest()[:16]


def add_source(conn: sqlite3.Connection, s: SourceIn) -> tuple[int, bool]:
    """Insert a source (deduped on kind+origin_id). Also records any
    contract addresses it mentions as unverified leads.
    Returns (source_id, created)."""
    if s.confidence not in CONFIDENCE:
        raise ValueError(f"confidence must be one of {CONFIDENCE}")
    if s.origin_id:
        row = conn.execute("SELECT id FROM sources WHERE kind=? AND origin_id=?", (s.kind, s.origin_id)).fetchone()
        if row:
            return int(row["id"]), False
    now = utcnow()
    cur = conn.execute(
        """INSERT INTO sources(kind,origin_url,origin_id,author,channel,published_at,captured_at,excerpt,
               confidence,claim_key,cites_url,raw) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (s.kind, s.origin_url, s.origin_id, s.author, s.channel, s.published_at, now, s.excerpt[:2000],
         s.confidence, s.claim_key or claim_key_for(s.excerpt), s.cites_url,
         json.dumps(s.raw, default=str) if s.raw else None),
    )
    sid = int(cur.lastrowid)
    hint = chain_hint(s.excerpt)
    for a in extract_evm_addresses(s.excerpt):
        conn.execute("INSERT OR IGNORE INTO leads(source_id,address,chain_hint,created_at) VALUES(?,?,?,?)",
                     (sid, a, hint, now))
    return sid, True


def independent_origins(conn: sqlite3.Connection, claim_key: str) -> int:
    """Distinct root origins behind a claim. Five reposts of one rumor = 1."""
    roots = set()
    for r in conn.execute("SELECT origin_url, cites_url, author FROM sources WHERE claim_key=?", (claim_key,)):
        root = normalize_url(r["cites_url"]) or normalize_url(r["origin_url"]) or f"author:{r['author']}"
        roots.add(root)
    return len(roots)


def verify_leads(conn: sqlite3.Connection, pons: PonsV2, limit: int = 50) -> list[dict[str, Any]]:
    """Check every unverified lead against Robinhood Chain + the Pons V2
    factory. A lead whose text points at another chain is rejected even if
    an address happens to exist here, because the same address can be a
    different contract on a different chain."""
    results = []
    for lead in rows(conn, "SELECT * FROM leads WHERE status='unverified' ORDER BY id LIMIT ?", (limit,)):
        hint = lead["chain_hint"]
        if hint and hint != "robinhood":
            status, note, token = "rejected", f"source refers to {hint}, not Robinhood Chain", None
        else:
            ok, note, rec = pons.verify_token(lead["address"])
            status, token = ("verified", rec.token) if ok and rec else ("rejected", None)
        conn.execute("UPDATE leads SET status=?, verified_token=?, verify_note=?, verified_at=? WHERE id=?",
                     (status, token, note, utcnow(), lead["id"]))
        journal_append(conn, "lead_verified" if status == "verified" else "lead_rejected",
                       {"lead_id": lead["id"], "address": lead["address"], "note": note,
                        "source_id": lead["source_id"]}, ref=token or lead["address"])
        results.append({"id": lead["id"], "status": status, "note": note})
    return results


def evidence_for_token(conn: sqlite3.Connection, token: str, limit: int = 20) -> list[dict[str, Any]]:
    """Sources tied to a token only through a verified lead."""
    return rows(conn, """
        SELECT s.* FROM sources s JOIN leads l ON l.source_id = s.id
        WHERE l.status='verified' AND lower(l.verified_token)=lower(?)
        ORDER BY s.id DESC LIMIT ?""", (token, limit))
