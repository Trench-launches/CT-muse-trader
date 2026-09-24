"""The website's "Ask Muse" chat.

A visitor sends a message, usually a contract address or a $ticker. MOOSE
researches that token live (the same onchain checks and screen the trader
uses, plus what it has heard and decided), and Muse answers from those facts.

Every request is a paid Muse call, so it is rate limited per visitor (salted
IP hash, never the raw IP) and capped overall per day. The global cap is the
hard ceiling on spend.
"""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any

from ..chain.pons import Phase
from ..db import kv_get, kv_set, rows, utcnow
from ..sources.store import evidence_for_token
from ..strategy.screen import activity_for, screen_almost_bonded, screen_migrated

ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")
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
        raise ChatLimited("Muse has answered all it can for today. Try again tomorrow.", 3600)
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


def find_token(conn: Any, text: str) -> tuple[str | None, str | None]:
    """(address, how) for the token the message is about: a pasted contract wins, else a $ticker
    MOOSE has indexed (the most recent launch with that symbol)."""
    m = ADDRESS.search(text)
    if m:
        return m.group(0), "address"
    t = TICKER.search(text)
    if t:
        r = conn.execute("SELECT token FROM launches WHERE lower(symbol)=lower(?) ORDER BY block_number DESC LIMIT 1",
                         (t.group(1),)).fetchone()
        if r:
            return r["token"], "ticker"
        return None, f"no Robinhood Chain launch with the ticker ${t.group(1)} in MOOSE's index"
    return None, None


def research(ctx: Any, token: str) -> dict[str, Any]:
    """Live facts about one token for Muse. Cached briefly so a burst of questions about the
    same coin doesn't repeat the chain reads."""
    key = token.lower()
    hit = _RESEARCH.get(key)
    if hit and time.time() - hit[1] < RESEARCH_TTL_S:
        return hit[0]
    facts = _research(ctx, token)
    _RESEARCH[key] = (facts, time.time())
    return facts


def _research(ctx: Any, token: str) -> dict[str, Any]:
    from ..strategy.engine import _market_cap
    from ..strategy.outcomes import spot_price
    s, conn = ctx.settings, ctx.conn
    ok, why, rec = ctx.pons.verify_token(token)
    if not ok or rec is None:
        return {"address": token, "is_pons_v2_launch_on_robinhood_chain": False, "why_not": why}
    token = rec.token
    meta = ctx.pons.token_meta(token)
    launch = (rows(conn, "SELECT * FROM launches WHERE lower(token)=lower(?)", (token,)) or [None])[0]
    if launch and not launch.get("symbol") and meta.symbol:  # so a later $ticker question finds it
        conn.execute("UPDATE launches SET name=?, symbol=?, decimals=? WHERE token=?",
                     (meta.name, meta.symbol, meta.decimals, launch["token"]))
    quote = (launch or {}).get("quote_symbol") or ("ETH" if rec.pair_token.lower() == "0x" + "0" * 40 else "other")
    f: dict[str, Any] = {"address": token, "is_pons_v2_launch_on_robinhood_chain": True, "name": meta.name,
                         "symbol": meta.symbol, "priced_in": quote, "creator_tax_pct": rec.creator_tax_bps / 100,
                         "explorer": f"{s.explorer_url}/token/{token}",
                         "logo": f"/logo/{token}" if (launch or {}).get("logo_url") else None}
    stage = {Phase.NOT_GRADUATED: "on the bonding curve", Phase.POOL_CREATED: "graduated to its Pons V4 pool"}
    f["stage"] = stage.get(rec.phase, f"{rec.phase.name.lower().replace('_', ' ')} (not tradable by MOOSE)")
    try:
        price, _ = spot_price(ctx, token)
        f["market_cap_usd"] = _market_cap(ctx, token, price) or "unknown"
    except Exception:
        f["market_cap_usd"] = "unknown"
    try:
        if rec.phase == Phase.NOT_GRADUATED:
            st = ctx.pons.curve_state(rec.curve)
            f["curve_progress_pct"] = round(st.progress * 100, 1)
            f["curve_fee_pct"] = st.fee_bps / 100
            if launch:
                head = ctx.rpc.block_number()
                bt = float((kv_get(conn, "block_time_s") or {}).get("value", 0.1))
                act = activity_for(conn, token, launch["deployer"], head, bt, s.activity_windows_s)
                c = screen_almost_bonded(s, token=token, progress=st.progress, phase=int(rec.phase),
                                         launch_age_s=(head - launch["block_number"]) * bt, snipe_window_s=0,
                                         fee_bps=st.fee_bps, creator_tax_bps=st.creator_tax_bps, activity=act,
                                         quote_symbol=quote)
                f["onchain_activity"] = {"windows": [asdict(w) for w in act.windows],
                                         "top_buyer_share": round(act.top_buyer_share, 3),
                                         "deployer_buy_share": round(act.deployer_buy_share, 3),
                                         "wallets_that_bought_and_sold_fast": act.roundtrip_traders,
                                         "accelerating": act.accelerating}
                f["moose_screen"] = {"flags": c.flags, "red_flags": c.blockers}
            else:
                f["onchain_activity"] = "not indexed by MOOSE yet"
        elif rec.phase == Phase.POOL_CREATED:
            pid = (launch or {}).get("pool_id") or ctx.pons.genuine_pool_id(rec)
            pool = ctx.market.genuine_pool_stats(token, pid)
            if pool:
                f["pool"] = {"volume_1h_usd": pool.volume_h1_usd, "volume_24h_usd": pool.volume_h24_usd,
                             "liquidity_usd": pool.liquidity_usd, "trades_1h": pool.txns_h1}
                c = screen_migrated(s, token=token, phase=int(rec.phase), pool_found=True,
                                    volume_h1_usd=pool.volume_h1_usd, liquidity_usd=pool.liquidity_usd,
                                    volume_h24_usd=pool.volume_h24_usd, fee_bps=(launch or {}).get("fee_bps") or 100,
                                    creator_tax_bps=rec.creator_tax_bps, activity=None, quote_symbol=quote)
                f["moose_screen"] = {"flags": c.flags, "red_flags": c.blockers}
            else:
                f["pool"] = "no market data for the genuine pool yet"
    except Exception as exc:  # a missing figure must never break the answer
        f["data_gap"] = f"some live numbers could not be read ({type(exc).__name__})"
    att = kv_get(conn, f"x_attention:{token.lower()}")
    if att:
        f["x_mentions_24h"] = att.get("count_24h")
    f["moose_decisions"] = [{"when": d["ts"], "action": d["action"], "confidence": d["confidence"],
                             "thesis": (d["thesis"] or "")[:300]}
                            for d in rows(conn, "SELECT ts, action, confidence, thesis FROM decisions WHERE "
                                                "lower(token)=lower(?) AND model!='rule' ORDER BY id DESC LIMIT 3", (token,))]
    f["moose_trades"] = rows(conn, "SELECT mode, status, opened_at, closed_at, entry_quote AS entry_eth, "
                                   "pnl_quote AS pnl_eth, exit_reason FROM positions WHERE lower(token)=lower(?) "
                                   "ORDER BY id DESC LIMIT 3", (token,))
    return f


def token_card(facts: dict[str, Any]) -> dict[str, Any] | None:
    """What the website shows above Muse's reply."""
    if not facts.get("is_pons_v2_launch_on_robinhood_chain"):
        return None
    return {k: facts.get(k) for k in ("address", "name", "symbol", "stage", "curve_progress_pct", "market_cap_usd",
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
    """One line for the chat's research card, e.g. 'graduated · $48.2K MC · $16K liquidity · 3% fees'."""
    bits = [f"curve {f['curve_progress_pct']:.0f}%" if f.get("curve_progress_pct") is not None else
            "graduated" if "graduated" in str(f.get("stage", "")) else str(f.get("stage") or "")]
    if f.get("market_cap_usd") not in (None, "", "unknown"):
        bits.append(f"{f['market_cap_usd']} MC")
    pool = f.get("pool") if isinstance(f.get("pool"), dict) else {}
    if pool.get("liquidity_usd"):
        bits.append(f"{_usd(pool['liquidity_usd'])} liquidity")
    fee = (f.get("curve_fee_pct") or 0) + (f.get("creator_tax_pct") or 0)
    if fee:
        bits.append(f"{fee:g}% fees")
    flags = len((f.get("moose_screen") or {}).get("red_flags") or [])
    if flags:
        bits.append(f"{flags} red flag{'s' if flags != 1 else ''}")
    return " · ".join(b for b in bits if b)


def _clip(t: str, n: int) -> str:
    t = " ".join(str(t or "").split())
    return t if len(t) <= n else t[:n - 1].rsplit(" ", 1)[0] + "…"


def _x_allowed(ctx: Any) -> bool:
    """The chat's daily allowance of X profile look-ups, so visitors can't spend MOOSE's X budget."""
    key = "chat_x_lookups:" + datetime.now(timezone.utc).strftime("%Y-%m-%d")
    used = int(kv_get(ctx.conn, key) or 0)
    if used >= ctx.settings.chat_x_lookups_day:
        return False
    kv_set(ctx.conn, key, used + 1)
    return True


def answer_events(svc: dict[str, Any], s: Any, db_path: str, who: str, msg: str, history: list[dict[str, str]],
                  hint: str | None) -> Any:
    """Research the coin step by step, then ask Muse. Yields what the site shows live:
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

        # the coin: a pasted contract, else the one the site says we're on, else a $ticker, else one named earlier
        if hint and not ADDRESS.search(msg):
            token, how = hint, "context"
        else:
            token, how = find_token(conn, msg)
        for m in reversed(history):
            if token or how:
                break
            if m["role"] == "user":
                token, how = find_token(conn, m["text"])
        facts: dict[str, Any] = {}
        ev: list[dict[str, Any]] = []
        story: dict[str, Any] = {}
        if token or how:
            yield step("coin", "run")
        if token:
            facts = research(ctx, token)
            if not facts.get("is_pons_v2_launch_on_robinhood_chain"):
                yield step("coin", "fail", "not a Pons launch on Robinhood Chain")
            else:
                yield step("coin", "done", f"${facts.get('symbol') or '?'} · {_clip(facts.get('name') or '', 40)}")
                yield step("onchain", "done", onchain_detail(facts))
                ev = evidence(conn, facts["address"])
                yield step("lore", "run")
                story, looked = lore_mod.cached(ctx, facts["address"])
                if story is None:
                    story, looked = lore_mod.read_onchain(ctx, facts["address"]), False
                desc = story.get("description") or ""
                yield step("lore", "done" if desc else "skip", f"\u201c{_clip(desc, 110)}\u201d" if desc else "no description stored")
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
        log_request(conn, who, token, "asked")  # counted before the paid call, whatever happens next
        yield step("muse", "run")
        res = svc["brain"].chat(facts, ev, history + [{"role": "user", "text": msg}], story)
        if res.decision is None:
            yield step("muse", "fail")
            yield {"type": "error", "detail": "Muse didn't answer that one. Try again in a moment."}
            return
        yield step("muse", "done")
        yield {"type": "answer", "reply": res.decision.reply.strip()[:2000], "token": token_card(facts),
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

