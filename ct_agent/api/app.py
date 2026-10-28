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
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from ..chain.keys import is_pubkey
from ..chain.rpc import redact
from ..config import Settings, get_settings
from ..db import journal_append, journal_recent, journal_verify, kv_get, open_db, rows, utcnow
from ..execution.risk import RiskManager
from ..sources.store import SourceIn, add_source, independent_origins
from ..sources.twitch import TwitchSignatureError, handle_eventsub, ingest_transcript
from . import chat

BASE58_MINT = "^[1-9A-HJ-NP-Za-km-z]{32,44}$"


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


class ChatMsg(BaseModel):
    role: str = Field(pattern="^(user|grok|muse)$")  # "muse": pages opened before the Grok switch
    text: str = Field(max_length=2000)


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    history: list[ChatMsg] = Field(default_factory=list, max_length=20)
    # a coin the visitor picked (a suggestion chip): researched with the live card
    token: str | None = Field(default=None, pattern=BASE58_MINT)
    # the coin discussed earlier: passed to Grok quietly, used only if the new message is about it
    context_token: str | None = Field(default=None, pattern=BASE58_MINT)


class PostIn(BaseModel):
    text: str = Field(min_length=1, max_length=280)
    image: str | None = None  # a file in ct_agent/social/media, e.g. "moose-cap.png"


class ImportLaunchIn(BaseModel):
    mint: str = Field(max_length=44)
    handle: str | None = Field(default=None, max_length=16)       # who to credit; VIGIL's own X handle by default
    fee_share_tx: str | None = Field(default=None, max_length=100)  # the split's transaction, for the record


class ResolveIn(BaseModel):
    status: str = Field(pattern="^(sent|pending|cancelled)$")
    x_post_id: str | None = None


def create_app(settings: Settings | None = None) -> FastAPI:
    s = settings or get_settings()
    app = FastAPI(title="CT Agent API", version="0.1.0",
                  description="Solana memecoin research and trading agent. Automated account; not financial advice.")
    open_db(s.database_path).close()  # run migrations once at startup
    # The public website reads the API from the browser, and posts to /chat. No cookies, and no
    # Authorization header allowed cross-origin, so admin calls can't come from a browser page.
    app.add_middleware(CORSMiddleware, allow_origins=s.site_origins, allow_methods=["GET", "POST"],
                       allow_headers=["content-type"])

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
                "rpc": redact(s.solana_rpc_url), "jupiter": bool(s.jupiter_api_key), "grok": bool(s.grok_api_key),
                "x_read": bool(s.x_bearer_token),
                "x_post": bool(s.x_user_access_token or (s.x_client_id and s.x_refresh_token)) and s.x_posting_enabled,
                "twitch": bool(s.twitch_client_id and s.twitch_eventsub_secret), "news_feeds": len(s.news_feeds),
                "live_trading_ready": s.trading_mode.value == "live" and s.live_execution_reviewed
                and bool(s.trading_wallet_private_key),
            },
            "counts": {t: count(t) for t in ("tokens", "scanner_calls", "sources", "leads", "decisions",
                                             "positions", "posts")},
            "cursors": {"scanner": kv_get(conn, "scanner_cursor"), "fibbot": kv_get(conn, "fibbot_cursor")},
        }

    # --- market & research --------------------------------------------------

    @app.get("/candidates")
    def candidates(conn=Depends(db)) -> list[dict[str, Any]]:
        return kv_get(conn, "candidates") or []

    @app.get("/tokens")
    @app.get("/launches")  # the old name, kept for existing links
    def token_list(stage: str | None = None, limit: int = 50, conn=Depends(db)) -> list[dict[str, Any]]:
        """Coins VIGIL has looked at (the token cache), newest first. stage: curve | pool."""
        limit = min(limit, 500)
        if stage in ("curve", "pool"):
            return rows(conn, "SELECT * FROM tokens WHERE stage=? ORDER BY first_seen DESC LIMIT ?", (stage, limit))
        return rows(conn, "SELECT * FROM tokens ORDER BY first_seen DESC LIMIT ?", (limit,))

    @app.get("/tokens/{token}")
    def token_detail(token: str, conn=Depends(db)) -> dict[str, Any]:
        row = rows(conn, "SELECT * FROM tokens WHERE mint=?", (token,))
        if not row:
            raise HTTPException(404, "unknown token")
        t = row[0]["mint"]
        return {
            "token": row[0],
            "explorer": f"{s.explorer_url}/token/{t}",
            "market": rows(conn, "SELECT id,ts,source,pool_address,price_usd,volume_h1_usd,volume_h24_usd,liquidity_usd,"
                                 "txns_h1 FROM market_snapshots WHERE token=? ORDER BY id DESC LIMIT 50", (t,)),
            "calls": rows(conn, "SELECT id, called_at, call_price_usd, status, note, last_move_pct, peak_move_pct "
                                "FROM scanner_calls WHERE token=? ORDER BY id DESC LIMIT 20", (t,)),
            "decisions": _decode(rows(conn, "SELECT * FROM decisions WHERE token=? ORDER BY id DESC LIMIT 20", (t,))),
            "positions": rows(conn, "SELECT * FROM positions WHERE token=? ORDER BY id DESC", (t,)),
            "evidence": rows(conn, "SELECT s.id,s.kind,s.origin_url,s.author,s.channel,s.published_at,s.confidence,"
                                   "s.excerpt,l.status lead_status FROM sources s JOIN leads l ON l.source_id=s.id "
                                   "WHERE l.address=? ORDER BY s.id DESC LIMIT 50", (t,)),
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

    @app.get("/scanner")
    def scanner_calls(limit: int = 50, conn=Depends(db)) -> dict[str, Any]:
        """The smart-money scanner's Solana calls as VIGIL sees them: moved how far, and what it did."""
        calls = rows(conn, "SELECT id, token, symbol, called_at, call_price_usd, status, note, last_move_pct, "
                           "peak_move_pct, triggered_at, checked_at, decision_id FROM scanner_calls "
                           "ORDER BY id DESC LIMIT ?", (min(limit, 500),))
        return {"source": "scanner", "cursor": kv_get(conn, "scanner_cursor"),
                "trigger_pct": s.scanner_trigger_pct, "window_hours": s.scanner_window_hours, "calls": calls}

    @app.get("/launchpad/status")
    def launch_status(symbol: str, wallet: str = "", conn=Depends(db)) -> dict[str, Any]:
        """Where a launch request for this ticker is, for the website's confirmation screen: the newest request in
        the last 2 hours (not seen yet, refused and why, approved, launched with its mint). Tickers can repeat, so
        the fee wallet from the form picks out the visitor's own request. All of it is public on X."""
        from datetime import datetime, timedelta, timezone
        since = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds")
        q = ("SELECT r.id, r.status, r.reason, r.symbol, r.name, r.handle, r.token, r.tx_hash, r.fee_shares, "
             "r.launched_at, p.x_post_id AS announce_x_id FROM launch_requests r LEFT JOIN posts p "
             "ON p.idempotency_key='launch_announce:' || r.id AND p.status='sent' "
             "WHERE upper(r.symbol)=? AND r.created_at>=?")
        args: list[Any] = [symbol.strip().lstrip("$").upper()[:10], since]
        if wallet:  # refused requests have no wallet on record: they still match on the ticker
            q += " AND (r.recipient=? OR r.recipient IS NULL)"  # base58: matched exactly, never lowercased
            args.append(wallet.strip())
        r = conn.execute(q + " ORDER BY r.id DESC LIMIT 1", args).fetchone()
        if not r:
            return {"status": "not_seen"}
        out = dict(r)
        out.pop("id")
        out["fee_shares"] = _json(out["fee_shares"])
        if out["status"] == "refused":  # the short public reason VIGIL replied with, not the internal note
            out["reason"] = (out["reason"] or "").removeprefix("grok declined: ")
        return out

    @app.get("/launchpad/recent")
    def launch_recent(limit: int = 8, conn=Depends(db)) -> list[dict[str, Any]]:
        """Coins the launchpad launched, newest first (the same list /site carries)."""
        from .site import recent_launches
        return recent_launches(conn, min(limit, 50))

    @app.get("/launchpad/board")
    def launch_board(conn=Depends(db)) -> dict[str, Any]:
        """The launchpad dashboard: stats, every launched coin with its curve progress and price, and the latest
        creator-fee payouts. Chain and market reads are cached for a minute and never fail the request."""
        import base64
        from ..launchpad.pumpfun import bonding_curve_pda
        from .site import launch_board_cached

        def curves(mints: list[str]) -> dict[str, bytes]:  # one getMultipleAccounts read for every coin's curve
            accs = chat_services()["rpc"].multiple_accounts([str(bonding_curve_pda(m)) for m in mints], encoding="base64")
            return {m: base64.b64decode(a["data"][0]) for m, a in zip(mints, accs) if a}

        return launch_board_cached(conn, s, curves, lambda mints: chat_services()["market"].prices_usd(mints))

    @app.get("/positions")
    def positions(status: str | None = None, mode: str | None = None, conn=Depends(db)) -> list[dict[str, Any]]:
        q, a = "SELECT p.*, t.symbol FROM positions p LEFT JOIN tokens t ON t.mint=p.token WHERE 1=1", []
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
                "net_pnl_sol": round(sum(c["pnl_quote"] or 0 for c in closed), 6),
                "open_exposure_sol": round(risk.exposure_sol(mode), 6),
                "paper_cash_sol": round(risk.paper_cash_sol(), 6) if mode == "paper" else None}

    @app.get("/site")
    def site(response: Response, conn=Depends(db)) -> dict[str, Any]:
        """Everything the public website shows, in one call."""
        from .site import site_data
        response.headers["Cache-Control"] = "public, max-age=15"
        return site_data(conn, s)

    @app.get("/logo/{token}")
    def logo(token: str, conn=Depends(db)) -> Response:
        """A tracked token's own logo, re-encoded as a small WebP. 404 if it has none."""
        from pathlib import Path
        from .logos import logo_file
        if not is_pubkey(token):
            raise HTTPException(404, "not a token address")
        path = logo_file(conn, Path(s.database_path).parent, token)
        if path is None:
            raise HTTPException(404, "no logo")
        return Response(path.read_bytes(), media_type="image/webp",
                        headers={"Cache-Control": "public, max-age=86400", "X-Content-Type-Options": "nosniff"})

    @app.get("/outcomes")
    def outcomes(hours: float | None = None, conn=Depends(db)) -> dict[str, Any]:
        """What happened after each call, split by the variables VIGIL saw."""
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

    # --- ask VIGIL (public chat, answered by Grok) ---------------------------------------------

    import threading
    chat_lock = threading.Lock()

    def chat_services() -> dict[str, Any]:
        """Chain, market and Grok clients for the chat, built once. Tests can preset app.state.chat_services."""
        svc = getattr(app.state, "chat_services", None)
        if svc is None:
            with chat_lock:  # the start-up warm-up and a first visitor must not build it twice
                svc = getattr(app.state, "chat_services", None)
                if svc is None:
                    from ..context import build
                    ctx = build(s)
                    ctx.conn.close()  # each request brings its own connection
                    svc = app.state.chat_services = {"rpc": ctx.rpc, "market": ctx.market, "jupiter": ctx.jupiter,
                                                     "brain": ctx.brain, "x": ctx.x, "web": ctx.web}
        return svc

    if s.chat_enabled and s.grok_api_key:  # building the clients takes ~10s: do it at start-up, not on a visitor's first question
        threading.Thread(target=chat_services, name="chat-warmup", daemon=True).start()

    def visitor(request: Request, conn: Any) -> str:
        # Behind Railway's proxy the client address is the last X-Forwarded-For entry (earlier ones
        # can be typed by the client). It is only ever stored as a salted hash.
        fwd = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
        ip = fwd[-1] if fwd else (request.client.host if request.client else "unknown")
        return chat.visitor_id(conn, ip)

    @app.get("/chat/limits")
    def chat_limits(request: Request, conn=Depends(db)) -> dict[str, Any]:
        return {"enabled": s.chat_enabled, "max_chars": s.chat_max_chars,
                "remaining": chat.remaining(conn, s, visitor(request, conn))}

    def chat_start(body: ChatIn, request: Request, conn: Any) -> tuple[Any, ...]:
        """Checks shared by both chat endpoints; raises the HTTP error a visitor should see."""
        if not s.chat_enabled:
            raise HTTPException(503, "The chat is switched off right now.")
        msg = body.message.strip()
        if len(msg) > s.chat_max_chars:
            raise HTTPException(400, f"Keep it under {s.chat_max_chars} characters.")
        who = visitor(request, conn)
        try:
            chat.check_limits(conn, s, who)
        except chat.ChatLimited as exc:
            raise HTTPException(429, str(exc), headers={"Retry-After": str(exc.retry_after_s)})
        svc = chat_services()
        if not svc["brain"].enabled:
            raise HTTPException(503, "VIGIL isn't connected right now.")
        history = [{"role": m.role, "text": m.text[:600]} for m in body.history[-8:]]
        return svc, who, msg, history

    @app.post("/chat")
    def ask_vigil(body: ChatIn, request: Request, conn=Depends(db)) -> dict[str, Any]:
        """Ask VIGIL (Grok) about a Solana coin. Rate limited per visitor and per day overall."""
        svc, who, msg, history = chat_start(body, request, conn)
        for e in chat.answer_events(svc, s, s.database_path, who, msg, history, body.token, body.context_token):
            if e["type"] == "error":
                raise HTTPException(502, e["detail"])
            if e["type"] == "answer":
                return {k: v for k, v in e.items() if k != "type"}
        raise HTTPException(502, "VIGIL didn't answer that one. Try again in a moment.")

    @app.post("/chat/stream")
    def ask_vigil_live(body: ChatIn, request: Request, conn=Depends(db)) -> StreamingResponse:
        """The same as /chat, but each research step is sent as it happens (one JSON object per line)."""
        svc, who, msg, history = chat_start(body, request, conn)
        events = chat.answer_events(svc, s, s.database_path, who, msg, history, body.token, body.context_token)
        return StreamingResponse((json.dumps(e, default=str) + "\n" for e in events), media_type="application/x-ndjson",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

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

    @app.post("/admin/posts")
    def queue_post(body: PostIn, who: str = Depends(admin), conn=Depends(db)) -> dict[str, Any]:
        """Queue a hand-written post (a thesis, an announcement). It goes out through the normal outbox:
        same daily cap, retries and journal as every other post. The same text is only ever queued once."""
        import hashlib
        from ..social.compose import Draft
        from ..social.outbox import enqueue, media_path
        text = body.text.strip()
        if body.image and media_path(body.image) is None:
            raise HTTPException(400, f"no image called {body.image!r} in ct_agent/social/media")
        key = "note:" + hashlib.sha256((text + (body.image or "")).encode()).hexdigest()[:16]
        pid, created = enqueue(conn, Draft("note", text, "", key, media=(body.image,) if body.image else ()))
        journal_append(conn, "post_queued_by_hand", {"post_id": pid, "by": who, "text": text, "image": body.image,
                                                     "new": created})
        return {"id": pid, "queued": created}

    @app.post("/admin/launches/import")
    def import_launch(body: ImportLaunchIn, who: str = Depends(admin), conn=Depends(db)) -> dict[str, Any]:
        """Put a launch made outside the X flow (ctagent launch-test) on the launch list, so the site shows it.
        The coin is checked on chain: its fee split must be locked and include VIGIL's launcher."""
        from ..launchpad.pumpfun import LaunchBlocked, PumpLauncher
        from ..launchpad.service import import_launch as do_import
        from . import site as site_mod
        if not is_pubkey(body.mint):
            raise HTTPException(400, "mint isn't a Solana address")
        launcher = PumpLauncher(s, chat_services()["rpc"], conn)
        try:
            out = do_import(conn, s, launcher, body.mint, handle=body.handle, fee_share_tx=body.fee_share_tx)
        except LaunchBlocked as exc:
            raise HTTPException(409, str(exc))
        site_mod._BOARD.clear()
        site_mod._CACHE.clear()  # the site shows it on its next poll, not a minute later
        return {**out, "by": who}

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


def _json(v: Any) -> Any:
    try:
        return json.loads(v) if isinstance(v, str) and v else v
    except json.JSONDecodeError:
        return v


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
