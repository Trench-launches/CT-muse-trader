"""The website's "Ask VIGIL" chat, answered by Grok.

A visitor sends a message, usually a Solana mint address or a $ticker. VIGIL
researches that coin live (the same mint-safety checks, holder read and screen
the trader uses, plus what it has heard and decided), and Grok answers from
those facts.

Every request is a paid Grok call, so it is rate limited per visitor (salted
IP hash, never the raw IP) and capped overall per day. The global cap is the
hard ceiling on spend.
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from ..chain import token as tok
from ..chain.keys import SOL_MINT, find_pubkeys
from ..db import kv_get, kv_set, rows, utcnow
from ..sources.store import evidence_for_token
from ..strategy.screen import screen_token

log = logging.getLogger(__name__)
DEXSCREENER_SEARCH = "https://api.dexscreener.com/latest/dex/search"
TICKER = re.compile(r"\$([A-Za-z][A-Za-z0-9]{0,19})\b")
_RESEARCH: dict[str, tuple[dict[str, Any], float]] = {}
RESEARCH_TTL_S = 60


class ChatLimited(Exception):
    def __init__(self, message: str, retry_after_s: int):
        super().__init__(message)
        self.retry_after_s = retry_after_s


def _since(**kw: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat(timespec="seconds")


def visitor_id(conn: Any, ip: str) -> str:
    """Salted hash of the visitor's IP. The salt is random per install and never leaves the database."""
    salt = kv_get(conn, "chat_salt")
    if not salt:
        salt = hashlib.sha256(f"{time.time_ns()}{id(conn)}".encode()).hexdigest()
        kv_set(conn, "chat_salt", salt)
    return hashlib.sha256(f"{salt}:{ip}".encode()).hexdigest()[:24]


def remaining(conn: Any, s: Any, who: str) -> dict[str, int]:
    hour = conn.execute("SELECT COUNT(*) FROM chat_requests WHERE who=? AND ts>=?", (who, _since(hours=1))).fetchone()[0]
    day = conn.execute("SELECT COUNT(*) FROM chat_requests WHERE who=? AND ts>=?", (who, _since(days=1))).fetchone()[0]
    return {"hour": max(0, s.chat_per_ip_hour - hour), "day": max(0, s.chat_per_ip_day - day)}


def check_limits(conn: Any, s: Any, who: str) -> None:
    """Raise ChatLimited if this visitor, or the site as a whole, is out of questions."""
    total = conn.execute("SELECT COUNT(*) FROM chat_requests WHERE ts>=?", (_since(days=1),)).fetchone()[0]
    if total >= s.chat_global_day:
        raise ChatLimited("VIGIL has answered all it can for today. Try again tomorrow.", 3600)
    left = remaining(conn, s, who)
    if left["day"] <= 0:
        raise ChatLimited(f"That's your {s.chat_per_ip_day} questions for today. Come back tomorrow.", 3600)
    if left["hour"] <= 0:
        first = conn.execute("SELECT MIN(ts) FROM chat_requests WHERE who=? AND ts>=?",
                             (who, _since(hours=1))).fetchone()[0]
        wait = 3600 - int((datetime.now(timezone.utc) - datetime.fromisoformat(first)).total_seconds()) if first else 600
        mins = max(1, round(wait / 60))
        raise ChatLimited(f"That's {s.chat_per_ip_hour} questions this hour. Try again in about {mins} min.", max(60, wait))


def log_request(conn: Any, who: str, token: str | None, status: str) -> None:
    conn.execute("INSERT INTO chat_requests(ts,who,token,status) VALUES(?,?,?,?)", (utcnow(), who, token, status))


def _mint_in(text: str) -> str | None:
    """The first Solana address in the text that isn't SOL itself."""
    return next((m for m in find_pubkeys(text or "") if m != SOL_MINT), None)


def _search_ticker(market: Any, ticker: str) -> str | None:
    """DexScreener search: the most liquid Solana coin with exactly this ticker."""
    try:
        r = market._http.get(DEXSCREENER_SEARCH, params={"q": ticker}, timeout=8)
        r.raise_for_status()
        pairs = [p for p in (r.json() or {}).get("pairs") or [] if p.get("chainId") == "solana"
                 and str((p.get("baseToken") or {}).get("symbol") or "").upper() == ticker.upper()]
    except Exception as exc:  # the visitor just gets "not found"
        log.info("ticker search for %s failed: %s", ticker, exc)
        return None
    if not pairs:
        return None
    best = max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))
    return (best.get("baseToken") or {}).get("address")


def find_token(conn: Any, text: str, market: Any = None) -> tuple[str | None, str | None]:
    """(mint, how) for the coin the message is about: a pasted mint wins, else a $ticker from the scanner's
    recent calls or VIGIL's token cache, else DexScreener's search."""
    m = _mint_in(text)
    if m:
        return m, "address"
    t = TICKER.search(text)
    if t:
        sym = t.group(1)
        r = conn.execute("SELECT token FROM scanner_calls WHERE upper(trim(replace(symbol, '\U0001f331', '')))=upper(?) "
                         "ORDER BY id DESC LIMIT 1", (sym,)).fetchone() \
            or conn.execute("SELECT mint AS token FROM tokens WHERE upper(symbol)=upper(?) ORDER BY last_checked DESC "
                            "LIMIT 1", (sym,)).fetchone()
        if r:
            return r["token"], "ticker"
        found = _search_ticker(market, sym) if market is not None else None
        if found:
            return found, "ticker"
        return None, f"no Solana coin with the ticker ${sym} found"
    return None, None


def research(ctx: Any, token: str) -> dict[str, Any]:
    """Live facts about one coin for Grok. Cached briefly so a burst of questions about the
    same coin doesn't repeat the chain reads."""
    hit = _RESEARCH.get(token)
    if hit and time.time() - hit[1] < RESEARCH_TTL_S:
        return hit[0]
    facts = _research(ctx, token)
    _RESEARCH[token] = (facts, time.time())
    return facts


STAGE_WORDS = {"curve": "on its launchpad bonding curve (e.g. pump.fun)", "pool": "graduated to a pool"}


def _research(ctx: Any, token: str) -> dict[str, Any]:
    from ..market import tokens
    s, conn = ctx.settings, ctx.conn
    try:
        info = tok.mint_info(ctx.rpc, token)
    except Exception as exc:
        return {"address": token, "is_solana_token": False, "why_not": f"couldn't read the chain ({type(exc).__name__})"}
    if not info.exists:
        return {"address": token, "is_solana_token": False, "why_not": "not a Solana token mint"}
    try:
        pool = ctx.market.pool_stats(token)
    except Exception:
        pool = None
    row = tokens.ensure(ctx, token, info=info, pool=pool) or {}
    if pool is None:
        row = {**row, "stage": None, "dex_id": None}
    safety = tok.safety_blockers(info, s.max_transfer_fee_bps)
    f: dict[str, Any] = {
        "address": token, "is_solana_token": True, "name": row.get("name"), "symbol": row.get("symbol"),
        "token_program": "Token-2022" if info.is_token_2022 else "SPL Token",
        "explorer": f"{s.explorer_url}/token/{token}",
        "logo": f"/logo/{token}" if row.get("logo_url") else None,
        "stage": row.get("stage"), "where_it_trades": STAGE_WORDS.get(row.get("stage") or "", "no pool found yet"),
        "dex": row.get("dex_id"),
        "liquidity_usd": round(pool.liquidity_usd) if pool and pool.liquidity_usd else None,
        "mcap_usd": round(pool.market_cap_usd) if pool and pool.market_cap_usd else None,
        "mint_safety": {"red_flags": safety, "creator_can_mint_more": bool(info.mint_authority),
                        "creator_can_freeze_coins": bool(info.freeze_authority)},
    }
    hs = None
    try:
        hs = tok.holder_facts(ctx.rpc, token, supply=info.supply, creator=row.get("creator"))
        if hs.get("error"):
            hs = None
    except Exception as exc:  # a missing figure must never break the answer
        f["data_gap"] = f"holders could not be read ({type(exc).__name__})"
    if hs:
        f["holders"] = hs
    if pool:
        f["pool"] = {"volume_1h_usd": pool.volume_h1_usd, "volume_24h_usd": pool.volume_h24_usd,
                     "liquidity_usd": pool.liquidity_usd, "trades_1h": pool.txns_h1, "price_usd": pool.price_usd}
    c = screen_token(s, token=token, stage=row.get("stage"), pool_found=pool is not None,
                     volume_h1_usd=pool.volume_h1_usd if pool else None,
                     liquidity_usd=pool.liquidity_usd if pool else None,
                     volume_h24_usd=pool.volume_h24_usd if pool else None, safety=safety, holders=hs)
    f["vigil_screen"] = {"flags": c.flags, "red_flags": c.blockers}
    att = kv_get(conn, f"x_attention:{token}")
    if att:
        f["x_mentions_24h"] = att.get("count_24h")
    call = conn.execute("SELECT called_at, last_move_pct, status, note FROM scanner_calls WHERE token=? "
                        "ORDER BY id DESC LIMIT 1", (token,)).fetchone()
    if call:
        f["smart_money_call"] = dict(call)
    f["vigil_decisions"] = [{"when": d["ts"], "action": d["action"], "confidence": d["confidence"],
                             "thesis": (d["thesis"] or "")[:300]}
                            for d in rows(conn, "SELECT ts, action, confidence, thesis FROM decisions WHERE "
                                                "token=? AND model!='rule' ORDER BY id DESC LIMIT 3", (token,))]
    f["vigil_trades"] = rows(conn, "SELECT mode, status, opened_at, closed_at, entry_quote AS entry_sol, "
                                   "pnl_quote AS pnl_sol, exit_reason FROM positions WHERE token=? "
                                   "ORDER BY id DESC LIMIT 3", (token,))
    return f


def token_card(facts: dict[str, Any]) -> dict[str, Any] | None:
    """What the website shows above Grok's reply."""
    if not facts.get("is_solana_token"):
        return None
    return {k: facts.get(k) for k in ("address", "name", "symbol", "stage", "dex", "liquidity_usd", "mcap_usd",
                                      "explorer", "logo")}


def _usd(v: Any) -> str:
    if not isinstance(v, (int, float)) or not v:
        return ""
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if v >= div:
            return f"${v / div:.3g}{suf}"
    return f"${v:.0f}"


def _count(n: Any) -> str:
    if not isinstance(n, (int, float)):
        return ""
    return _usd(n).lstrip("$") if n >= 1000 else str(int(n))


def _age(iso: str | None) -> str:
    try:
        d = (datetime.now(timezone.utc) - datetime.fromisoformat(str(iso).replace("Z", "+00:00"))).days
    except (TypeError, ValueError):
        return ""
    if d < 1:
        return "new today"
    if d < 60:
        return f"{d} day{'s' if d != 1 else ''} old"
    return f"{d // 30} months old" if d < 730 else f"{d // 365} years old"


def onchain_detail(f: dict[str, Any]) -> str:
    """One line for the chat's research card, e.g. 'pool on pumpswap · $48.2K MC · $16K liquidity · top 10 hold 20%'."""
    stage = f.get("stage")
    bits = ["on the bonding curve" if stage == "curve" else (f"pool on {f['dex']}" if f.get("dex") else "graduated")
            if stage == "pool" else "no pool yet"]
    if f.get("mcap_usd"):
        bits.append(f"{_usd(f['mcap_usd'])} MC")
    if f.get("liquidity_usd"):
        bits.append(f"{_usd(f['liquidity_usd'])} liquidity")
    hs = f.get("holders") or {}
    if hs.get("top10_share") is not None:
        bits.append(f"top 10 hold {hs['top10_share']:.0%}")
    flags = len((f.get("vigil_screen") or {}).get("red_flags") or [])
    if flags:
        bits.append(f"{flags} red flag{'s' if flags != 1 else ''}")
    return " · ".join(b for b in bits if b)


def _clip(t: str, n: int) -> str:
    t = " ".join(str(t or "").split())
    return t if len(t) <= n else t[:n - 1].rsplit(" ", 1)[0] + "…"


def _x_allowed(ctx: Any) -> bool:
    """The chat's daily allowance of X profile look-ups, so visitors can't spend VIGIL's X budget."""
    key = "chat_x_lookups:" + datetime.now(timezone.utc).strftime("%Y-%m-%d")
    used = int(kv_get(ctx.conn, key) or 0)
    if used >= ctx.settings.chat_x_lookups_day:
        return False
    kv_set(ctx.conn, key, used + 1)
    return True


def answer_events(svc: dict[str, Any], s: Any, db_path: str, who: str, msg: str, history: list[dict[str, str]],
                  hint: str | None, context: str | None = None) -> Any:
    """Research the coin step by step, then ask Grok. Yields what the site shows live:
    {"type": "step", "id", "status": run|done|skip|fail, "detail"}, then {"type": "answer", ...}
    or {"type": "error", "detail"}."""
    from types import SimpleNamespace
    from ..db import open_db
    from ..sources import lore as lore_mod
    conn = open_db(db_path)  # a streamed reply outlives the request, so it keeps its own connection
    try:
        ctx = SimpleNamespace(settings=s, conn=conn, **svc)
        if svc.get("x") is not None:  # the shared X client counts its budget in the database: give it this request's connection
            import copy
            ctx.x = copy.copy(svc["x"])
            ctx.x.conn = conn

        def step(i: str, st: str, d: str = "") -> dict[str, Any]:
            return {"type": "step", "id": i, "status": st, "detail": d}

        # A coin gets researched (with the live card) only when this message names one: a pasted contract, a
        # $ticker, or a suggestion chip. Otherwise the coin from earlier rides along quietly, so a real follow-up
        # ("and the fees?") still works, but an unrelated question isn't answered with that coin's research.
        market = svc.get("market")
        if hint and not _mint_in(msg):
            token, how = hint, "chip"
        else:
            token, how = find_token(conn, msg, market)
        earlier = None
        if not token and not how:
            earlier = context
            for m in reversed(history):
                if earlier:
                    break
                if m["role"] == "user":
                    earlier = find_token(conn, m["text"])[0]  # no web search for old messages
        facts: dict[str, Any] = {}
        ev: list[dict[str, Any]] = []
        story: dict[str, Any] = {}
        if token or how:
            yield step("coin", "run")
        if token:
            facts = research(ctx, token)
            if not facts.get("is_solana_token"):
                yield step("coin", "fail", "not a Solana token")
            else:
                yield step("coin", "done", f"${facts.get('symbol') or '?'} · {_clip(facts.get('name') or '', 40)}")
                yield step("onchain", "done", onchain_detail(facts))
                ev = evidence(conn, facts["address"])
                yield step("lore", "run")
                story, looked = lore_mod.cached(ctx, facts["address"])
                if story is None:
                    story, looked = lore_mod.read_onchain(ctx, facts["address"]), False
                desc = story.get("description") or ""
                yield step("lore", "done" if desc else "skip", f"\u201c{_clip(desc, 110)}\u201d" if desc else "no description published")
                links = story.get("links") or {}
                handle = links.get("x")
                if not handle:
                    yield step("x", "skip", "no X account linked")
                else:
                    if not looked:
                        if getattr(ctx, "x", None) is not None and _x_allowed(ctx):
                            yield step("x", "run", f"@{handle}")
                            story["x_account"] = lore_mod.x_profile(ctx.x, handle)
                        else:
                            story["x_account"] = None
                    xa = story.get("x_account")
                    if xa:
                        f = xa.get("followers")
                        bits = [f"@{xa.get('handle') or handle}", f"{_count(f)} followers" if f is not None else "",
                                _age(xa.get("created_at")), "verified" if xa.get("verified") else ""]
                        yield step("x", "done", " · ".join(b for b in bits if b))
                    else:
                        yield step("x", "skip", f"@{handle} (profile not checked)")
                site = links.get("website")
                if not site:
                    yield step("website", "skip", "no website linked")
                else:
                    if not looked:
                        yield step("website", "run", site)
                        web = getattr(ctx, "web", None)
                        story["website"] = lore_mod.site_summary(web, site) if web is not None else None
                    w = story.get("website")
                    yield step("website", "done" if w else "fail",
                               _clip((w or {}).get("title") or site, 80) if w else f"couldn't open {site}")
                if not looked:
                    lore_mod.save(ctx, facts["address"], story, True)
        elif how:
            yield step("coin", "fail", how)
            facts = {"lookup": how}
        elif earlier:  # quietly, from the caches: no research card, no coin card
            facts = research(ctx, earlier)
            if facts.get("is_solana_token"):
                facts["context_only"] = True
                ev = evidence(conn, facts["address"])
                story = lore_mod.cached(ctx, facts["address"])[0] or lore_mod.read_onchain(ctx, facts["address"])
            else:
                facts = {}
        facts["now_utc"] = datetime.now(timezone.utc).strftime("%A %d %B %Y, %H:%M UTC")
        log_request(conn, who, token, "asked")  # counted before the paid call, whatever happens next
        yield step("grok", "run")
        res = svc["brain"].chat(facts, ev, history + [{"role": "user", "text": msg}], story)
        if res.decision is None:
            yield step("grok", "fail")
            yield {"type": "error", "detail": "VIGIL didn't answer that one. Try again in a moment."}
            return
        yield step("grok", "done")
        yield {"type": "answer", "reply": res.decision.reply.strip()[:2000], "token": token_card(facts) if token else None,
               "remaining": remaining(conn, s, who)}
    finally:
        conn.close()


def lore(ctx: Any, token: str) -> dict[str, Any]:
    from ..sources.lore import get_lore
    try:
        return get_lore(ctx, token)
    except Exception:
        return {}


def evidence(conn: Any, token: str) -> list[dict[str, Any]]:
    return [{k: e.get(k) for k in ("id", "kind", "author", "channel", "published_at", "confidence", "origin_url",
                                   "excerpt")} for e in evidence_for_token(conn, token, 6)]

