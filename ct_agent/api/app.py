"""HTTP API: the backend for the public dashboard plus admin controls.

Reads are public by design (the brief wants every trade and source
visible). Anything that changes state needs the admin bearer token, and is
disabled entirely when CT_API_ADMIN_TOKEN is unset.
"""
from __future__ import annotations

import hmac
import json
from typing import Any, Iterator

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from ..config import Settings, get_settings
from ..db import journal_append, journal_recent, journal_verify, kv_get, open_db, rows, utcnow
from ..execution.risk import RiskManager
from ..sources.store import SourceIn, add_source, independent_origins
from ..sources.twitch import TwitchSignatureError, handle_eventsub, ingest_transcript


class KillIn(BaseModel):
    on: bool
    reason: str = Field(min_length=3)


class NoteIn(BaseModel):
    excerpt: str = Field(min_length=3)
    origin_url: str | None = None
    author: str | None = None
    published_at: str | None = None
    confidence: str = "reported"
    cites_url: str | None = None
    claim_key: str | None = None
    token_address: str | None = Field(default=None, description="links via a lead; still verified onchain")


class TranscriptIn(BaseModel):
    channel: str
    url: str
    offset_s: int | None = None
    excerpt: str = Field(min_length=3)
    confidence: str = "rumor"
    spoken_at: str | None = None


class ResolveIn(BaseModel):
    status: str = Field(pattern="^(sent|pending|cancelled)$")
    x_post_id: str | None = None


def create_app(settings: Settings | None = None) -> FastAPI:
    s = settings or get_settings()
    app = FastAPI(title="CT Agent API", version="0.1.0",
                  description="Robinhood Chain research and paper-trading agent. Automated account; not financial advice.")
    open_db(s.database_path).close()  # run migrations once at startup
    # The public website reads the API from the browser. Reads only: no cookies, no admin calls.
    app.add_middleware(CORSMiddleware, allow_origins=s.site_origins, allow_methods=["GET"], allow_headers=[])

    def db() -> Iterator[Any]:
        conn = open_db(s.database_path)
        try:
            yield conn
        finally:
            conn.close()

    def admin(authorization: str = Header(default="")) -> str:
        if not s.api_admin_token:
            raise HTTPException(403, "admin endpoints disabled: set CT_API_ADMIN_TOKEN")
        token = authorization.removeprefix("Bearer ").strip()
        if not hmac.compare_digest(token, s.api_admin_token):
            raise HTTPException(401, "bad admin token")
        return "admin"

    # --- status -------------------------------------------------------------

    @app.get("/health")
    def health(conn=Depends(db)) -> dict[str, Any]:
        killed, why = RiskManager(s, conn).kill_active()
        ok, bad = journal_verify(conn)
        count = lambda t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]  # noqa: E731
        return {
            "agent": s.agent_name, "automated_account": True, "mode": s.trading_mode.value,
            "kill_switch": {"on": killed, "reason": why},
            "journal": {"entries": count("journal"), "chain_intact": ok, "first_bad_id": bad},
            "integrations": {
                "rpc": s.rpc_url.split("/v2/")[0], "muse": bool(s.muse_api_key), "x_read": bool(s.x_bearer_token),
                "x_post": bool(s.x_user_access_token or (s.x_client_id and s.x_refresh_token)) and s.x_posting_enabled,
                "twitch": bool(s.twitch_client_id and s.twitch_eventsub_secret), "news_feeds": len(s.news_feeds),
                "live_trading_ready": s.trading_mode.value == "live" and s.live_execution_reviewed
                and bool(s.trading_wallet_private_key),
            },
            "counts": {t: count(t) for t in ("launches", "curve_trades", "sources", "leads", "decisions",
                                             "positions", "posts")},
            "cursors": {"launch": kv_get(conn, "launch_cursor"), "trade": kv_get(conn, "trade_cursor")},
        }

    # --- market & research --------------------------------------------------

    @app.get("/candidates")
    def candidates(conn=Depends(db)) -> list[dict[str, Any]]:
        return kv_get(conn, "candidates") or []

    @app.get("/launches")
    def launches(stage: str | None = None, limit: int = 50, conn=Depends(db)) -> list[dict[str, Any]]:
        limit = min(limit, 500)
        if stage == "almost_bonded":
            return rows(conn, "SELECT * FROM launches WHERE last_phase=0 AND last_progress>=? "
                              "ORDER BY last_progress DESC LIMIT ?", (s.almost_bonded_ratio, limit))
        if stage == "migrated":
            return rows(conn, "SELECT * FROM launches WHERE last_phase=2 ORDER BY block_number DESC LIMIT ?", (limit,))
        return rows(conn, "SELECT * FROM launches ORDER BY block_number DESC LIMIT ?", (limit,))

    @app.get("/tokens/{token}")
    def token_detail(token: str, conn=Depends(db)) -> dict[str, Any]:
        launch = rows(conn, "SELECT * FROM launches WHERE lower(token)=lower(?)", (token,))
        if not launch:
            raise HTTPException(404, "unknown token")
        t = launch[0]["token"]
        return {
            "launch": launch[0],
            "explorer": f"{s.explorer_url}/token/{t}",
            "snapshots": rows(conn, "SELECT * FROM token_snapshots WHERE token=? ORDER BY id DESC LIMIT 50", (t,)),
            "market": rows(conn, "SELECT id,ts,source,pool_address,price_usd,volume_h1_usd,volume_h24_usd,liquidity_usd,"
                                 "txns_h1 FROM market_snapshots WHERE token=? ORDER BY id DESC LIMIT 50", (t,)),
            "decisions": _decode(rows(conn, "SELECT * FROM decisions WHERE token=? ORDER BY id DESC LIMIT 20", (t,))),
            "positions": rows(conn, "SELECT * FROM positions WHERE token=? ORDER BY id DESC", (t,)),
            "evidence": rows(conn, "SELECT s.id,s.kind,s.origin_url,s.author,s.channel,s.published_at,s.confidence,"
                                   "s.excerpt,l.status lead_status FROM sources s JOIN leads l ON l.source_id=s.id "
                                   "WHERE lower(l.address)=lower(?) ORDER BY s.id DESC LIMIT 50", (t,)),
            "journal": journal_recent(conn, ref=t, limit=50),
        }

    @app.get("/sources")
    def sources(kind: str | None = None, limit: int = 100, conn=Depends(db)) -> list[dict[str, Any]]:
        q, a = "SELECT id,kind,origin_url,author,channel,published_at,captured_at,confidence,claim_key,cites_url,excerpt " \
               "FROM sources", []
        if kind:
            q += " WHERE kind=?"
            a.append(kind)
        a.append(min(limit, 500))
        out = rows(conn, q + " ORDER BY id DESC LIMIT ?", a)
        for r in out:
            r["independent_origins"] = independent_origins(conn, r["claim_key"]) if r["claim_key"] else 1
        return out

    @app.get("/leads")
    def leads(status: str | None = None, conn=Depends(db)) -> list[dict[str, Any]]:
        if status:
            return rows(conn, "SELECT * FROM leads WHERE status=? ORDER BY id DESC LIMIT 200", (status,))
        return rows(conn, "SELECT * FROM leads ORDER BY id DESC LIMIT 200")

    # --- trading ------------------------------------------------------------

    @app.get("/decisions")
    def decisions(limit: int = 50, conn=Depends(db)) -> list[dict[str, Any]]:
        return _decode(rows(conn, "SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (min(limit, 500),)))

    @app.get("/positions")
    def positions(status: str | None = None, mode: str | None = None, conn=Depends(db)) -> list[dict[str, Any]]:
        q, a = "SELECT p.*, l.symbol FROM positions p LEFT JOIN launches l ON l.token=p.token WHERE 1=1", []
        if status:
            q += " AND p.status=?"
            a.append(status)
        if mode:
            q += " AND p.mode=?"
            a.append(mode)
        return rows(conn, q + " ORDER BY p.id DESC", a)

    @app.get("/scorecard")
    def scorecard(mode: str = "paper", conn=Depends(db)) -> dict[str, Any]:
        closed = rows(conn, "SELECT * FROM positions WHERE mode=? AND status='closed'", (mode,))
        wins = sum(1 for c in closed if (c["pnl_quote"] or 0) > 0)
        risk = RiskManager(s, conn)
        return {"mode": mode, "label": "PAPER (simulated, not onchain)" if mode == "paper" else "LIVE",
                "closed": len(closed), "wins": wins, "losses": len(closed) - wins,
                "net_pnl_eth": round(sum(c["pnl_quote"] or 0 for c in closed), 6),
                "open_exposure_eth": round(risk.exposure_eth(mode), 6),
                "paper_cash_eth": round(risk.paper_cash_eth(), 6) if mode == "paper" else None}

    @app.get("/site")
    def site(response: Response, conn=Depends(db)) -> dict[str, Any]:
        """Everything the public website shows, in one call."""
        from .site import site_data
        response.headers["Cache-Control"] = "public, max-age=15"
        return site_data(conn, s)

    @app.get("/logo/{token}")
    def logo(token: str, conn=Depends(db)) -> Response:
        """A tracked token's own logo, re-encoded as a small WebP. 404 if it has none."""
        import re
        from pathlib import Path
        from .logos import logo_file
        if not re.fullmatch(r"0x[0-9a-fA-F]{40}", token):
            raise HTTPException(404, "not a token address")
        path = logo_file(conn, Path(s.database_path).parent, token)
        if path is None:
            raise HTTPException(404, "no logo")
        return Response(path.read_bytes(), media_type="image/webp",
                        headers={"Cache-Control": "public, max-age=86400", "X-Content-Type-Options": "nosniff"})

    @app.get("/outcomes")
    def outcomes(hours: float | None = None, conn=Depends(db)) -> dict[str, Any]:
        """What happened after each call, split by the variables MOOSE saw."""
        from ..strategy.outcomes import report
        return report(conn, hours)

    @app.get("/journal")
    def journal(kind: str | None = None, ref: str | None = None, limit: int = 100, conn=Depends(db)) -> list[dict]:
        return journal_recent(conn, kind=kind, ref=ref, limit=min(limit, 1000))

    @app.get("/journal/verify")
    def journal_check(conn=Depends(db)) -> dict[str, Any]:
        ok, bad = journal_verify(conn)
        return {"chain_intact": ok, "first_bad_id": bad}

    @app.get("/posts")
    def posts(status: str | None = None, conn=Depends(db)) -> list[dict[str, Any]]:
        if status:
            return rows(conn, "SELECT * FROM posts WHERE status=? ORDER BY id DESC LIMIT 200", (status,))
        return rows(conn, "SELECT * FROM posts ORDER BY id DESC LIMIT 200")

    # --- admin --------------------------------------------------------------

    @app.post("/admin/kill")
    def kill(body: KillIn, who: str = Depends(admin), conn=Depends(db)) -> dict[str, Any]:
        RiskManager(s, conn).set_kill(body.on, body.reason, who)
        return {"kill_switch": body.on}

    @app.post("/admin/sources")
    def add_note(body: NoteIn, who: str = Depends(admin), conn=Depends(db)) -> dict[str, Any]:
        text = body.excerpt + (f" {body.token_address}" if body.token_address else "")
        sid, created = add_source(conn, SourceIn(kind="manual", excerpt=text, confidence=body.confidence,
                                                 origin_url=body.origin_url, origin_id=None, author=body.author,
                                                 published_at=body.published_at, claim_key=body.claim_key,
                                                 cites_url=body.cites_url or body.origin_url))
        journal_append(conn, "manual_note", {"source_id": sid, "by": who})
        return {"source_id": sid, "created": created}

    @app.post("/admin/transcripts")
    def add_transcript(body: TranscriptIn, _: str = Depends(admin), conn=Depends(db)) -> dict[str, Any]:
        return {"source_id": ingest_transcript(conn, channel=body.channel, url=body.url, offset_s=body.offset_s,
                                               excerpt=body.excerpt, confidence=body.confidence,
                                               spoken_at=body.spoken_at)}

    @app.post("/admin/posts/{post_id}/resolve")
    def resolve_post(post_id: int, body: ResolveIn, who: str = Depends(admin), conn=Depends(db)) -> dict[str, Any]:
        """For posts stuck in 'unknown' or 'failed': a human checks X and says
        whether it went out (sent + id), should be retried (pending) or dropped."""
        row = conn.execute("SELECT status FROM posts WHERE id=?", (post_id,)).fetchone()
        if not row:
            raise HTTPException(404, "no such post")
        if row["status"] not in ("unknown", "failed", "pending"):
            raise HTTPException(409, f"post is {row['status']}")
        conn.execute("UPDATE posts SET status=?, x_post_id=COALESCE(?, x_post_id), sent_at=CASE WHEN ?='sent' "
                     "THEN ? ELSE sent_at END WHERE id=?", (body.status, body.x_post_id, body.status, utcnow(), post_id))
        journal_append(conn, "post_resolved", {"post_id": post_id, "status": body.status, "by": who})
        return {"id": post_id, "status": body.status}

    # --- twitch webhook -----------------------------------------------------

    @app.post("/webhooks/twitch")
    async def twitch(request: Request, conn=Depends(db)) -> Response:
        body = await request.body()
        try:
            payload = json.loads(body or b"{}")
            status, text = handle_eventsub(conn, s.twitch_eventsub_secret, dict(request.headers), body, payload)
        except TwitchSignatureError as exc:
            raise HTTPException(403, str(exc))
        except json.JSONDecodeError:
            raise HTTPException(400, "bad json")
        return Response(content=text, status_code=status, media_type="text/plain")

    return app


def _decode(rs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for r in rs:
        for k in ("reasons", "risks", "source_ids"):
            if r.get(k):
                try:
                    r[k] = json.loads(r[k])
                except (TypeError, json.JSONDecodeError):
                    pass
        r.pop("raw", None)
    return rs
