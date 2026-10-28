"""The trading engine: smart-money calls in, Grok's read, a rule-based buy, rule-based exits, posts out.

Coins come from the scanner (sources/scanner.py). Grok analyses every new call and its read is posted, but the
trade itself is a rule: a call is bought once the coin is up scanner_trigger_pct from the call (or the moment it
lands, in buy-on-call mode). Nothing Grok says can force a buy: the kill switch, the risk limits and the price
impact bound all sit between the rule and the swap. Exits are fixed rules too: a stop loss, a take-profit ladder
(the stop moves to entry after the first rung) and a maximum hold.

Every trade is one swap path on Solana: SOL for the coin and back, through Jupiter. Amounts are kept in whole SOL
and whole tokens (floats); each coin's decimals come from the token cache.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from ..chain import token as tok
from ..chain.keys import SOL_MINT
from ..chain.rpc import RpcError
from ..context import Context
from ..db import journal_append, journal_recent, kv_get, kv_set, rows, tx, utcnow
from ..execution.jupiter import JupiterError
from ..execution.live import LiveTradingBlocked
from ..execution.paper import close_position, open_position, take_partial
from ..execution.pricing import FillEstimate, from_quote, pool_estimate
from ..market import tokens
from ..social import compose
from ..social.outbox import enqueue
from ..sources.store import evidence_for_token, independent_origins
from ..units import QUOTE_SYMBOL, from_base, lamports, sol, to_base
from .outcomes import record_call
from .screen import Candidate, screen_token

log = logging.getLogger(__name__)
EST_FEE_BPS = 100  # pool fee assumed by the indicative estimate (pump.fun's curve takes about 1%)


@dataclass
class Assessed:
    cand: Candidate
    coin: dict[str, Any]                 # the token cache row (symbol, decimals, stage, dex_id, ...)
    pool: Any = None                     # PoolStats of the coin's deepest SOL pool, if any
    sol_usd: float | None = None
    call: dict[str, Any] | None = None   # the scanner call this came from
    info: tok.MintInfo | None = None
    holders: dict[str, Any] | None = None


def _symbol(ctx: Context, token: str) -> str:
    row = ctx.conn.execute("SELECT symbol FROM tokens WHERE mint=?", (token,)).fetchone()
    return (row["symbol"] if row else None) or "?"


def _decimals(ctx: Context, token: str) -> int:
    return tokens.decimals(ctx, token)


# --- assessing a call -------------------------------------------------------

def _assess_call(ctx: Context, call: dict[str, Any], sol_usd: float | None, *, with_holders: bool = False) \
        -> Assessed | None:
    """Read one called coin: its mint, its deepest SOL pool, the screen (and its holders, for an analysis)."""
    s, conn = ctx.settings, ctx.conn
    mint = call["token"]
    try:
        info = tok.mint_info(ctx.rpc, mint)
    except RpcError as exc:
        log.warning("mint read for %s failed: %s", mint, exc)
        return None
    if not info.exists:
        return None
    try:
        pool = ctx.market.pool_stats(mint)
    except Exception as exc:  # market data is indicative: the buy itself is priced by a Jupiter quote
        log.warning("pool stats for %s unavailable: %s", mint, exc)
        pool = None
    coin = tokens.ensure(ctx, mint, info=info, pool=pool) or {"mint": mint, "decimals": info.decimals}
    if info.decimals is not None:
        kv_set(conn, f"supply:{mint}", from_base(info.supply, info.decimals))
    if pool:
        conn.execute("INSERT INTO market_snapshots(token,ts,source,pool_address,price_usd,volume_h1_usd,"
                     "volume_h24_usd,liquidity_usd,txns_h1) VALUES(?,?,?,?,?,?,?,?,?)",
                     (mint, utcnow(), pool.source, pool.pool_id, pool.price_usd, pool.volume_h1_usd,
                      pool.volume_h24_usd, pool.liquidity_usd, pool.txns_h1))
    holders = None
    if with_holders:
        try:
            holders = tok.holder_facts(ctx.rpc, mint, supply=info.supply, creator=coin.get("creator"))
            if holders.get("error"):
                holders = None
        except Exception as exc:  # a failed read never blocks an analysis; the gate just can't apply
            log.warning("holders unavailable for %s: %s", mint, exc)
    c = screen_token(s, token=mint, stage=coin.get("stage"), pool_found=pool is not None,
                     volume_h1_usd=pool.volume_h1_usd if pool else None,
                     liquidity_usd=pool.liquidity_usd if pool else None,
                     volume_h24_usd=pool.volume_h24_usd if pool else None,
                     safety=tok.safety_blockers(info, s.max_transfer_fee_bps), holders=holders)
    if call.get("last_move_pct") is not None:
        c.metrics["move_since_call_pct"] = call["last_move_pct"]
    return Assessed(c, coin, pool, sol_usd, call, info, holders)


def scanner_cycle(ctx: Context) -> list[dict[str, Any]]:
    """The smart-money scanner's calls. Grok analyses every new tradeable call and posts its read. The trade
    itself is a rule: a call is bought once the coin is up scanner_trigger_pct from the call price. Nothing
    about the coin can veto that; only the wallet's safety limits can (open trades, daily loss, balance)."""
    from ..sources import scanner
    s, conn = ctx.settings, ctx.conn
    try:
        if s.scanner_main_enabled:
            scanner.fetch_new(ctx)
    except Exception as exc:  # Supabase down: keep working the calls already copied
        log.warning("scanner fetch failed: %s", exc)
    try:
        scanner.fetch_fibbot(ctx)
    except Exception as exc:  # Fibbot's Supabase down: the main scanner's calls still come through
        log.warning("fibbot fetch failed: %s", exc)
    ready = scanner.refresh(ctx)
    sol_usd = ctx.market.sol_usd()
    out: list[dict[str, Any]] = []
    ready_ids = {c["id"] for c in ready}
    # calls that arrive already up get analysed first, so the buy post can use Grok's read
    fresh = [c for c in ready if not c.get("decision_id")] + \
        [c for c in scanner.to_analyse(conn, s.scanner_window_hours) if c["id"] not in ready_ids]
    for call in fresh[:4]:
        if not call.get("pool_id"):  # VIGIL can't trade it, but it still gets a read and a post
            did = analyse_untradeable(ctx, call)
            out.append({"token": call["token"], "analysed": did, "tradeable": False})
            continue
        a = _assess_call(ctx, call, sol_usd, with_holders=True)
        if a:
            did = analyse_call(ctx, a)
            call["decision_id"] = did
            out.append({"token": a.cand.token, "analysed": did})
    if not s.scanner_buy_on_call:  # in buy-on-call mode the fast caller loop does the buying
        for call in ready:
            a = _assess_call(ctx, call, sol_usd)
            if a:
                out.append(buy_call(ctx, a))
    waiting = [{"token": c["token"], "symbol": c["symbol"], "stage": "scanner_call", "score": 0}
               for c in rows(conn, "SELECT token, symbol FROM scanner_calls WHERE status='watching' AND pool_id IS NOT NULL "
                                   "ORDER BY id DESC LIMIT 10")]
    kv_set(conn, "candidates", waiting)  # the research worker gathers X chatter on these
    return out


def _dex_stats(ctx: Context, token: str) -> dict[str, Any]:
    """The coin's deepest SOL pool on DexScreener, for a call VIGIL can't trade."""
    try:
        p = ctx.market.pool_stats(token)
    except Exception as exc:
        log.warning("market data unavailable for %s: %s", token, exc)
        return {}
    if not p:
        return {}
    raw = p.raw if isinstance(p.raw, dict) else {}
    return {"source": p.source, "dex": p.dex_id, "name": (raw.get("baseToken") or {}).get("name"),
            "price_usd": p.price_usd, "market_cap_usd": p.market_cap_usd, "liquidity_usd": p.liquidity_usd,
            "volume_h1_usd": p.volume_h1_usd, "volume_h24_usd": p.volume_h24_usd,
            "price_change_pct": raw.get("priceChange"), "txns": raw.get("txns"), "note": "aggregator data, indicative only"}


def analyse_untradeable(ctx: Context, call: dict[str, Any]) -> int:
    """A call VIGIL can't trade still gets Grok's read and a radar post, with what's knowable about it."""
    from ..sources.scanner import call_facts
    s = ctx.settings
    token = call["token"]
    symbol = (call.get("symbol") or "?").replace("\U0001f331", "").strip() or "?"
    facts = call_facts(call, s.scanner_trigger_pct)
    facts["rule"] = (f"VIGIL can't trade this coin ({call.get('note') or 'not tradeable'}), so it won't buy it. "
                     "Your job is only the analysis: what the coin is, why smart money may be in, what could go wrong.")
    pack = {"token": token, "symbol": symbol, "chain": "Solana", "tradeable_by_vigil": False,
            "why_not_tradeable": call.get("note"), "scanner_call": facts, "market": _dex_stats(ctx, token),
            "lore": _lore(ctx, token), "evidence": [], "limits": {"mode": s.trading_mode.value}}
    return _record_analysis(ctx, token, symbol, pack, call, None)


def analyse_call(ctx: Context, a: Assessed) -> int:
    """Grok's read on a new smart-money call: recorded, and posted to the radar. It does not decide the trade."""
    token, symbol = a.cand.token, a.coin.get("symbol") or (a.call or {}).get("symbol") or "?"
    return _record_analysis(ctx, token, symbol, build_pack(ctx, a), a.call, a)


def _record_analysis(ctx: Context, token: str, symbol: str, pack: dict[str, Any], call: dict[str, Any],
                     a: Assessed | None) -> int:
    from ..sources.scanner import mark_analysed
    s, conn = ctx.settings, ctx.conn
    tradeable = a is not None
    res = ctx.brain.decide(pack)
    d = res.decision
    thesis, reasons, risks = (d.thesis, d.reasons, d.risks) if d else ("", [res.error or "no analysis"], [])
    with tx(conn):
        cur = conn.execute("INSERT INTO decisions(ts,token,stage,action,confidence,thesis,reasons,risks,source_ids,"
                           "model,raw) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                           (utcnow(), token, "scanner_call", "watch", d.confidence if d else 0.0, thesis,
                            json.dumps(reasons), json.dumps(risks), json.dumps(d.evidence_ids if d else []),
                            res.model, res.raw))
        did = int(cur.lastrowid)
        journal_append(conn, "scanner_analysis", {"decision_id": did, "token": token, "symbol": symbol,
                                                  "grok_action": d.action if d else None,
                                                  "confidence": d.confidence if d else None, "thesis": thesis,
                                                  "reasons": reasons, "risks": risks,
                                                  "narrative": d.narrative.model_dump() if d else None,
                                                  "grok_error": res.error}, ref=token, in_tx=True)
    mark_analysed(conn, call["id"], did)
    if a is not None:
        try:
            record_call(ctx, a, decision_id=did, action="watch", confidence=d.confidence if d else 0.0, pack=pack,
                        gates=[], model=res.model)
        except Exception as exc:  # tracking is for analysis; it must never stop a trade
            log.warning("could not record call outcome for %s: %s", token, exc)
    if d and tradeable and s.scanner_buy_on_call:
        _post_read_under_buy(ctx, token, symbol, did, d)
    elif d:
        skip, parent = _thread(conn, token, "watch")
        if not skip:
            pb = d.post
            draft = compose.watch_note(symbol=symbol, token=token, decision_id=did, narrative=pb.narrative,
                                       confirmation=pb.confirmation, watching=pb.watching,
                                       trigger=f"it runs +{s.scanner_trigger_pct:g}% from the smart-money call"
                                       if tradeable else "",
                                       line=d.public_line or thesis)
            draft.reply_to = parent
            enqueue(conn, draft)
    return did


def _post_read_under_buy(ctx: Context, token: str, symbol: str, did: int, d: Any) -> None:
    """In buy-on-call mode the buy goes first; Grok's read on the coin follows as a reply under the BOUGHT post."""
    conn = ctx.conn
    pos = conn.execute("SELECT id, mode FROM positions WHERE token=? ORDER BY id DESC LIMIT 1", (token,)).fetchone()
    if not pos:
        return
    enqueue(conn, compose.buy_read(symbol=symbol, token=token, key=f"analysis:{did}",
                                   reply_to=f"entry:{pos['mode']}:{pos['id']}",
                                   why=d.public_thesis or d.public_line or d.thesis,
                                   reasons=d.public_reasons or d.reasons[:2],
                                   risk=d.public_risk or (d.risks or [""])[0]))


def caller_once(ctx: Context) -> list[dict[str, Any]]:
    """The fast loop (every few seconds): new smart-money calls are bought as they land. Grok's analysis and
    the rest of the trading cycle run in the slower trader loop."""
    from ..sources import scanner
    s = ctx.settings
    if not s.scanner_buy_on_call or ctx.risk.kill_active()[0]:
        return []
    try:
        if s.scanner_main_enabled:
            scanner.fetch_new(ctx)
        scanner.fetch_fibbot(ctx)
    except Exception as exc:  # a missed poll is retried in a few seconds
        log.warning("caller fetch failed: %s", exc)
    out = []
    ready = scanner.refresh(ctx)
    sol_usd = ctx.market.sol_usd() if ready else None  # cached for 5 minutes
    for call in ready:
        a = _assess_call(ctx, call, sol_usd)
        if a:
            out.append(buy_call(ctx, a))
    return out


def account_sol(ctx: Context) -> float | None:
    """What the account is worth for sizing: live, the wallet's SOL plus open trades at cost; paper, the
    starting balance plus closed P&L. None if the wallet can't be read."""
    s, risk = ctx.settings, ctx.risk
    if s.trading_mode.value == "paper":
        return risk.paper_cash_sol() + risk.exposure_sol("paper")
    if not ctx.live.wallet:
        return None
    try:
        return sol(ctx.rpc.get_balance(ctx.live.wallet)) + risk.exposure_sol("live")
    except Exception as exc:
        log.warning("wallet balance unavailable: %s", exc)
        return None


def _trade_size(ctx: Context) -> float | None:
    """position_size_pct of the account (capped by max_position_sol in the risk check), else None = the max."""
    pct = ctx.settings.position_size_pct
    if pct <= 0:
        return None
    acct = account_sol(ctx)
    return round(acct * pct / 100, 6) if acct else 0.0


def buy_call(ctx: Context, a: Assessed) -> dict[str, Any]:
    """The +30% rule fired: buy. Only the wallet's safety limits (and the price impact bound) can stop it."""
    s, conn = ctx.settings, ctx.conn
    token, symbol = a.cand.token, a.coin.get("symbol") or (a.call or {}).get("symbol") or "?"
    move = (a.call or {}).get("last_move_pct") or 0
    verdict = ctx.risk.check_entry(token=token, quote_symbol=QUOTE_SYMBOL, mode=s.trading_mode.value,
                                   desired_sol=_trade_size(ctx))
    gates = list(verdict.reasons)
    fill = None
    if not gates and s.trading_mode.value == "paper":
        fill = _fill_for(ctx, a, verdict.size_sol)
        if fill is None:
            gates.append("no price for a paper fill this cycle")  # live buys use their own Jupiter order
        elif fill.price_impact_bps > s.max_slippage_bps:
            gates.append(f"price impact {fill.price_impact_bps} bps > {s.max_slippage_bps}")
    if gates:
        journal_append(conn, "scanner_buy_held", {"token": token, "symbol": symbol, "move_pct": move, "why": gates},
                       ref=token)
        return {"token": token, "action": "held", "gates": gates}
    prior = conn.execute("SELECT thesis, risks FROM decisions WHERE id=?", (a.call.get("decision_id"),)).fetchone() \
        if a.call and a.call.get("decision_id") else None
    risks = (json.loads(prior["risks"] or "[]") if prior else []) or ["a memecoin can go to zero fast"]
    thesis = (prior["thesis"] if prior else "") or "smart money called it"
    reasons = (["bought on the smart-money call"] if s.scanner_buy_on_call
               else [f"up {move:.0f}% since the smart-money call (buy rule: +{s.scanner_trigger_pct:g}%)"])
    with tx(conn):
        cur = conn.execute("INSERT INTO decisions(ts,token,stage,action,confidence,thesis,reasons,risks,source_ids,"
                           "model,raw) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                           (utcnow(), token, "scanner_trigger", "buy", 1.0, thesis, json.dumps(reasons),
                            json.dumps(risks), "[]", "rule", ""))
        did = int(cur.lastrowid)
        journal_append(conn, "decision", {"decision_id": did, "token": token, "symbol": symbol, "stage": "scanner_trigger",
                                          "action": "buy", "rule": f"+{s.scanner_trigger_pct:g}% from call",
                                          "move_pct": move, "gates": [], "reasons": reasons}, ref=token, in_tx=True)
    res = _enter(ctx, a, did, fill, verdict.size_sol, [], risks, {"evidence": []},
                 catalyst="a smart-money call" if s.scanner_buy_on_call else f"a smart-money call, up {move:.0f}% since")
    return {"token": token, "decision_id": did, "action": "buy", **res}


# --- briefing pack ----------------------------------------------------------

def build_pack(ctx: Context, a: Assessed) -> dict[str, Any]:
    conn, s = ctx.conn, ctx.settings
    ev = evidence_for_token(conn, a.cand.token)
    for e in ev:
        e["independent_origins"] = independent_origins(conn, e["claim_key"]) if e.get("claim_key") else 1
    attention = kv_get(conn, f"x_attention:{a.cand.token}")
    past = [{"ts": j["ts"], "kind": j["kind"], "payload": j["payload"]}
            for j in journal_recent(conn, ref=a.cand.token, limit=10)]
    # losses since the scorecard reset only: the testing-phase trades before it aren't lessons for live trading
    mistakes = rows(conn, "SELECT token, exit_reason, pnl_quote, closed_at FROM positions WHERE status='closed' "
                          "AND pnl_quote<0 AND opened_at>=? ORDER BY id DESC LIMIT 5", (s.score_since,))
    info = a.info
    pack: dict[str, Any] = {
        "token": a.cand.token, "symbol": a.coin.get("symbol"), "name": a.coin.get("name"),
        "chain": "Solana", "quote_asset": QUOTE_SYMBOL,
        "where_it_trades": {"stage": a.cand.stage, "dex": a.coin.get("dex_id"),
                            "meaning": {"curve": "still on its launchpad bonding curve (e.g. pump.fun)",
                                        "pool": "graduated to a pool (e.g. PumpSwap, Raydium)"}.get(a.cand.stage or "",
                                                                                                  "unknown")},
        "mint_safety": {"red_flags": tok.safety_blockers(info, s.max_transfer_fee_bps) if info else [],
                        "token_program": ("Token-2022" if info.is_token_2022 else "SPL Token") if info else None},
        "screen": {"score": a.cand.score, "flags": a.cand.flags, "blockers": a.cand.blockers,
                   "metrics": a.cand.metrics},
        "x_attention_24h": attention,
        "evidence": [{k: e.get(k) for k in ("id", "kind", "author", "channel", "published_at", "confidence",
                                             "origin_url", "excerpt", "independent_origins")} for e in ev],
        "past_journal_for_token": past,
        "recent_losing_trades": mistakes,
        "limits": {"max_position_sol": s.max_position_sol, "mode": s.trading_mode.value},
        "rules": {"verified_source_required": s.min_independent_sources_for_buy > 0},
        "market_cap_usd": (round(a.pool.market_cap_usd) if a.pool and a.pool.market_cap_usd else None),
        "holders": a.holders,
        "lore": _lore(ctx, a.cand.token),
    }
    if a.call:
        from ..sources.scanner import call_facts
        pack["scanner_call"] = call_facts(a.call, s.scanner_trigger_pct)
        if s.scanner_buy_on_call:
            pack["scanner_call"]["rule"] = (
                "VIGIL buys every smart-money call the moment it lands, and has ALREADY BOUGHT this one. Give your "
                "honest read: what the coin is, why smart money may be in, what could go wrong. Don't say you are "
                "watching, waiting or passing; the position is open.")
    if a.pool:
        pack["pool_indicative"] = {"source": a.pool.source, "dex": a.pool.dex_id, "volume_h1_usd": a.pool.volume_h1_usd,
                                   "volume_h24_usd": a.pool.volume_h24_usd, "liquidity_usd": a.pool.liquidity_usd,
                                   "trades_h1": a.pool.txns_h1, "price_sol": a.pool.price_native, "sol_usd": a.sol_usd,
                                   "note": "aggregator data, indicative only"}
    return pack


# --- posts ------------------------------------------------------------------

def _thread(conn, token: str, kind: str) -> tuple[bool, str | None]:
    """(skip, reply_to_key) for a new call on a token. The same call twice in
    24h is skipped; a different call (watch -> buy, watch -> pass) is posted as
    a reply under the previous one so each token reads as one thread."""
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
    prev = conn.execute("SELECT idempotency_key, kind FROM posts WHERE ref=? AND kind IN ('watch','pass','trade_entry') "
                        "AND status NOT IN ('failed','cancelled') AND created_at>=? ORDER BY id DESC LIMIT 1",
                        (token, since)).fetchone()
    if prev is None:
        return False, None
    if prev["kind"] == "trade_entry" and kind != "trade_entry":
        return True, None  # already in it: milestone updates and the exit cover the token now
    return prev["kind"] == kind and kind != "trade_entry", prev["idempotency_key"]


def _market_cap_usd(ctx: Context, token: str, price_sol: float | None) -> float | None:
    """Market cap in USD: price (SOL) x supply x SOL/USD. None when any part is unknown."""
    try:
        if not price_sol:
            return None
        supply = kv_get(ctx.conn, f"supply:{token}")
        if supply is None:
            info = tok.mint_info(ctx.rpc, token)
            if not info.exists:
                return None
            supply = from_base(info.supply, info.decimals)
            kv_set(ctx.conn, f"supply:{token}", supply)
        usd = ctx.market.sol_usd()
        return price_sol * supply * usd if usd else None
    except Exception as exc:  # a missing figure must never block a post or a trade
        log.warning("market cap unavailable for %s: %s", token, exc)
        return None


def _market_cap(ctx: Context, token: str, price_sol: float | None) -> str:
    """Live market cap for a post, e.g. '$42.1K'. '' when unknown, so a post never shows an invented figure."""
    mc = _market_cap_usd(ctx, token, price_sol)
    return compose.usd_compact(mc) if mc else ""


def _lore(ctx: Context, token: str) -> dict[str, Any]:
    """The coin's own story and links; never lets a failed look-up block a decision."""
    from ..sources.lore import get_lore
    try:
        return get_lore(ctx, token)
    except Exception as exc:
        log.warning("lore unavailable for %s: %s", token, exc)
        return {}


# --- entries ----------------------------------------------------------------

def _fill_for(ctx: Context, a: Assessed, size_sol: float) -> FillEstimate | None:
    """A paper buy of `size_sol`: a real Jupiter quote (what the swap would get right now), else an estimate from
    the pool's indicative liquidity. None if neither is available."""
    dec = int(a.coin.get("decimals") if a.coin.get("decimals") is not None else _decimals(ctx, a.cand.token))
    try:
        return from_quote(ctx.jupiter.order(SOL_MINT, a.cand.token, lamports(size_sol)), buy=True, decimals=dec)
    except (JupiterError, KeyError, ValueError) as exc:
        log.warning("jupiter quote for %s unavailable, estimating: %s", a.cand.token, exc)
    p = a.pool
    if p and p.price_native and p.liquidity_usd and a.sol_usd:
        return pool_estimate(size_sol, p.price_native, p.liquidity_usd, a.sol_usd, EST_FEE_BPS)
    return None


def _enter(ctx: Context, a: Assessed, did: int, fill: FillEstimate | None, size_sol: float, used: list[int],
           risks: list[str], pack: dict[str, Any], catalyst: str = "") -> dict[str, Any]:
    s, conn = ctx.settings, ctx.conn
    token, symbol = a.cand.token, a.coin.get("symbol") or (a.call or {}).get("symbol") or "?"
    mode = s.trading_mode.value
    if mode == "live":
        try:
            dec = _decimals(ctx, token)
            lf = ctx.live.swap(token, "buy", lamports(size_sol))
        except (LiveTradingBlocked, ValueError) as exc:
            journal_append(conn, "live_entry_blocked", {"token": token, "error": str(exc), "decision_id": did},
                           ref=token, mode="live")
            return {"entered": False, "error": str(exc)}
        got, spent = from_base(lf.token_amount, dec), sol(lf.quote_amount)
        entry_price = spent / got if got else 0
        with tx(conn):
            cur = conn.execute("INSERT INTO positions(token,mode,status,decision_id,quote_symbol,opened_at,entry_quote,"
                               "entry_tokens,entry_price) VALUES(?,?,?,?,?,?,?,?,?)",
                               (token, "live", "open", did, QUOTE_SYMBOL, utcnow(), spent, got, entry_price))
            pid = int(cur.lastrowid)
        spot = entry_price
    else:
        assert fill is not None
        pid = open_position(conn, token=token, symbol=symbol, decision_id=did, quote_symbol=QUOTE_SYMBOL, fill=fill,
                            stage=a.cand.stage or "unknown", meta={"evidence_ids": used})
        entry_price, spent, spot = fill.avg_price, fill.quote_in, fill.spot_price
    if a.call:
        from ..sources.scanner import mark_bought
        mark_bought(conn, token)
    cited = [e for e in pack["evidence"] if e["id"] in used]
    catalyst = catalyst or (_describe_source(cited[0]) if cited else "broad onchain buying")
    heat = a.cand.metrics.get("volume_heat")
    momentum = f"1h volume at {heat:.1f}x its daily average" if heat else ""
    d = compose.trade_entry(mode=mode, symbol=symbol, token=token, size=f"{spent:.4f} {QUOTE_SYMBOL}",
                            entry=f"{entry_price:.3g} {QUOTE_SYMBOL}", catalyst=catalyst, progress=None,
                            momentum=momentum, risk=risks[0], position_id=pid,
                            own_token=bool(s.agent_token_address) and token == s.agent_token_address,
                            mc=_market_cap(ctx, token, spot), plan=_exit_plan(s))
    d.reply_to = _thread(conn, token, "trade_entry")[1]  # under an earlier watch/pass, if any
    enqueue(conn, d)
    return {"entered": True, "position_id": pid}


def _exit_plan(s: Any) -> str:
    """e.g. '-70% stop · 50% at 1.5x · 25% at 2x · 24h max'"""
    rungs = [f"{f * 100:g}% at {m:g}x" for m, f in sorted(s.take_profit_ladder)]
    return " · ".join([f"-{s.stop_loss_pct:g}% stop", *rungs, f"{s.max_hold_hours:g}h max"])


def _describe_source(src: dict[str, Any]) -> str:
    """Human wording for the post, e.g. 'a first-party news post from decrypt.co'."""
    from urllib.parse import urlparse
    who = src.get("author") or src.get("channel") or urlparse(src.get("origin_url") or "").netloc.replace("www.", "")
    kind = {"news": "news post", "x": "post on X", "twitch_transcript": "stream", "twitch_chat": "stream chat",
            "manual": "source"}.get(src.get("kind"), "source")
    grade = "first-party " if src.get("confidence") == "first_party" else ""
    return f"a {grade}{kind}" + (f" from {who}" if who else "")


# --- exits ------------------------------------------------------------------

def _left(pos: dict[str, Any]) -> float:
    """Tokens still held: all of them until a take-profit rung sells some."""
    return pos["tokens_left"] if pos.get("tokens_left") is not None else pos["entry_tokens"]


def _mark_prices(ctx: Context, mints: list[str]) -> dict[str, tuple[float, float | None, float]]:
    """{mint: (SOL per token, USD liquidity or None, SOL/USD)} for every open position, from ONE batched Jupiter
    price call (the exit loop runs every few seconds, and Jupiter's free tier allows about one call a second).
    DexScreener covers whatever Jupiter can't price."""
    mints = [m for m in dict.fromkeys(mints) if m]
    out: dict[str, tuple[float, float | None, float]] = {}
    if not mints:
        return out
    try:
        px = ctx.jupiter.prices(mints + [SOL_MINT])
        sol_usd = float((px.get(SOL_MINT) or {}).get("usdPrice") or 0)
        if sol_usd > 0:
            for m in mints:
                p = px.get(m) or {}
                if p.get("usdPrice"):
                    liq = p.get("liquidity")
                    out[m] = (float(p["usdPrice"]) / sol_usd, float(liq) if liq else None, sol_usd)
    except Exception as exc:  # Jupiter down or rate limited: DexScreener below
        log.warning("jupiter prices unavailable: %s", exc)
    missing = [m for m in mints if m not in out]
    if missing:
        try:
            sol_usd = ctx.market.sol_usd()
            if sol_usd:
                for m, p in ctx.market.prices_usd(missing).items():
                    out[m] = (p / sol_usd, None, sol_usd)
        except Exception as exc:
            log.warning("dexscreener prices unavailable: %s", exc)
    return out


def _mark(pos: dict[str, Any], tokens_held: float, mark: tuple[float, float | None, float] | None) \
        -> FillEstimate | None:
    """What selling `tokens_held` would return at the current mark: the pool estimate when Jupiter reports the
    coin's liquidity (so a thin pool marks lower), else plain spot."""
    if not mark or not mark[0]:
        return None
    price, liq, sol_usd = mark
    if liq and sol_usd:
        return pool_estimate(tokens_held, price, liq, sol_usd, EST_FEE_BPS, buy=False)
    return FillEstimate(tokens_held * price, tokens_held, price, price, 0, 0.0, "jupiter_price")


def _paper_sell(ctx: Context, pos: dict[str, Any], tokens_sold: float, mark: FillEstimate) -> FillEstimate:
    """A paper sale priced by a real Jupiter quote for exactly this amount (only taken when actually selling);
    the mark is the fallback."""
    try:
        dec = _decimals(ctx, pos["token"])
        q = ctx.jupiter.order(pos["token"], SOL_MINT, to_base(tokens_sold, dec))
        return from_quote(q, buy=False, decimals=dec)
    except (JupiterError, KeyError, ValueError) as exc:
        log.warning("jupiter sell quote for %s unavailable, using the mark: %s", pos["token"], exc)
    if abs(mark.tokens_out - tokens_sold) < 1e-12:
        return mark
    scale = tokens_sold / mark.tokens_out if mark.tokens_out else 0
    return FillEstimate(mark.quote_in * scale, tokens_sold, mark.avg_price, mark.spot_price, mark.price_impact_bps,
                        mark.fee_and_tax_quote * scale, mark.method)


def _live_sell(ctx: Context, pos: dict[str, Any], tokens_sold: float) -> FillEstimate:
    """Sell `tokens_sold` onchain. Raises LiveTradingBlocked if it can't."""
    try:
        dec = _decimals(ctx, pos["token"])
    except ValueError as exc:
        raise LiveTradingBlocked(str(exc)) from exc
    lf = ctx.live.swap(pos["token"], "sell", to_base(tokens_sold, dec))
    got, sold = sol(lf.quote_amount), from_base(lf.token_amount, dec)
    return FillEstimate(got, sold, got / sold if sold else 0, 0, 0, 0, "onchain")


def _at(ctx: Context, token: str, fill: FillEstimate) -> str:
    """Where a sell happened, for the post: market cap if known, else the fill price."""
    mc = _market_cap(ctx, token, fill.spot_price or fill.avg_price)
    return f"{mc} MC" if mc else f"{fill.avg_price:.3g} {QUOTE_SYMBOL}"


def _post_mortem(ctx: Context, pos: dict[str, Any], reason: str, pnl_pct: float, age_h: float,
                 symbol: str) -> dict[str, str]:
    """Grok's honest read on a losing trade for the exit post, journalled so it isn't lost.
    Falls back to the original thesis alone if Grok can't answer."""
    conn = ctx.conn
    d = conn.execute("SELECT thesis, reasons, risks FROM decisions WHERE id=?", (pos["decision_id"],)).fetchone() \
        if pos.get("decision_id") else None
    buy = {"thesis": d["thesis"], "reasons": json.loads(d["reasons"] or "[]"), "risks": json.loads(d["risks"] or "[]")} if d else {}
    r = ctx.brain.review_loss({"symbol": symbol, "token": pos["token"], "entry_sol": pos["entry_quote"],
                               "pnl_pct": round(pnl_pct, 1), "held_hours": round(age_h, 1), "exit_reason": reason,
                               "buy_decision": buy, "marks": kv_get(conn, f"marks:{pos['id']}") or [],
                               "evidence": evidence_for_token(conn, pos["token"], 10)})
    out = {"bought": (buy.get("thesis") or "")}
    if r.decision:
        out = {"bought": r.decision.bought_because or out["bought"], "underestimated": r.decision.underestimated,
               "lesson": r.decision.lesson}
        journal_append(conn, "lesson", {"position_id": pos["id"], "token": pos["token"], "symbol": symbol,
                                        "pnl_pct": round(pnl_pct, 1), **out}, ref=pos["token"])
    return out


def _plan(s: Any, hits: int) -> str:
    """Where the exits are now, in words, for the milestone posts."""
    ladder = sorted(s.take_profit_ladder)
    if not hits:
        return f"Stop -{s.stop_loss_pct:.0f}%, first take-profit at {ladder[0][0]:g}x."
    if hits < len(ladder):
        return f"Stop at entry, next take-profit at {ladder[hits][0]:g}x."
    return f"Stop at entry, the rest rides for up to {s.max_hold_hours:.0f}h."


def manage_positions(ctx: Context) -> list[dict[str, Any]]:
    """Exits are fixed rules, no judgement calls: a stop loss, the take-profit rungs, and a timer. After the
    first rung the stop moves to entry; whatever is left when the timer runs out is sold. Every open position is
    marked from one batched price call; a real quote (paper) or swap (live) is only taken when something sells."""
    s, conn = ctx.settings, ctx.conn
    ladder = sorted(s.take_profit_ladder)
    results = []
    modes = ["paper", "live"] if s.trading_mode.value == "live" else ["paper"]
    open_pos = [p for m in modes for p in ctx.risk.open_positions(m)]
    marks_now = _mark_prices(ctx, [p["token"] for p in open_pos]) if open_pos else {}
    for pos in open_pos:
        left, hits = _left(pos), pos.get("tp_hits") or 0
        fill = _mark(pos, left, marks_now.get(pos["token"]))
        if fill is None:
            journal_append(conn, "mark_failed", {"position_id": pos["id"], "why": "no price"}, ref=pos["token"])
            continue
        realized = pos.get("realized_quote") or 0.0
        value = realized + fill.quote_in  # the whole trade: sold so far plus the rest at market
        pnl_pct = (value / pos["entry_quote"] - 1) * 100 if pos["entry_quote"] else 0
        # price multiple on what's still held: what it sells for now per token vs what it cost per token
        cost = pos["entry_quote"] / pos["entry_tokens"] if pos["entry_tokens"] else 0
        mult = (fill.quote_in / left) / cost if left and cost else 0
        # the site's sparkline: a point a minute (30 kept), with the newest one kept live on every check
        marks = kv_get(conn, f"marks:{pos['id']}") or []
        if marks and datetime.now(timezone.utc).timestamp() - (kv_get(conn, f"marks_at:{pos['id']}") or 0) < 60:
            marks[-1] = round(value, 8)
        else:
            marks = (marks + [round(value, 8)])[-30:]
            kv_set(conn, f"marks_at:{pos['id']}", datetime.now(timezone.utc).timestamp())
        kv_set(conn, f"marks:{pos['id']}", marks)
        age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(pos["opened_at"])).total_seconds() / 3600
        crossed = [i for i, (m, _) in enumerate(ladder) if i >= hits and mult >= m]
        symbol = _symbol(ctx, pos["token"])
        reason = why = None  # reason is the record; why is how the post says it ("...because <why>")
        if not hits and pnl_pct <= -s.stop_loss_pct:
            reason, why = f"stop loss at {pnl_pct:.1f}%", f"it hit my -{s.stop_loss_pct:.0f}% stop"
        elif hits and mult <= 1.0:
            reason = f"back to entry after taking profit at {ladder[0][0]:g}x"
            why = "the runner came back to my entry"
        elif crossed:
            top = crossed[-1]  # a jump past several rungs sells them all at once
            sell = min(left, pos["entry_tokens"] * sum(f for _, f in ladder[hits:top + 1]))
            if sell >= left * 0.999:  # the rungs add up to everything that's left
                reason, why = f"final take profit at {mult:.1f}x", f"it hit my final target at {mult:.1f}x"
            else:
                results.append(_take_profit(ctx, pos, fill, sell, top + 1, mult, symbol, ladder))
                continue
        elif age_h >= s.max_hold_hours:  # the timer covers the whole trade, the runner included
            reason = f"max hold {s.max_hold_hours:.0f}h reached"
            why = f"{s.max_hold_hours:.0f}h passed" + ("" if hits else " without the move")
        if not reason:
            _milestone_update(ctx, pos, pnl_pct, fill, hits)
            results.append({"position_id": pos["id"], "pnl_pct": round(pnl_pct, 2), "action": "hold"})
            continue
        if pos["mode"] == "paper":
            fill = _paper_sell(ctx, pos, left, fill)
            close_position(conn, pos, fill=fill, reason=reason, symbol=symbol)
        else:
            try:
                fill = _live_sell(ctx, pos, left)
                total = realized + fill.quote_in
                conn.execute("UPDATE positions SET status='closed', closed_at=?, exit_quote=?, exit_price=?, pnl_quote=?, "
                             "exit_reason=?, tokens_left=0 WHERE id=?",
                             (utcnow(), total, fill.avg_price, total - pos["entry_quote"], reason, pos["id"]))
            except LiveTradingBlocked as exc:
                journal_append(conn, "live_exit_blocked", {"position_id": pos["id"], "error": str(exc), "reason": reason},
                               ref=pos["token"], mode="live")
                results.append({"position_id": pos["id"], "action": "exit_blocked", "error": str(exc)})
                continue
        pnl_sol = realized + fill.quote_in - pos["entry_quote"]
        pnl_pct = pnl_sol / pos["entry_quote"] * 100 if pos["entry_quote"] else 0
        _post_later(ctx, "trade_exit", dict(mode=pos["mode"], symbol=symbol, token=pos["token"], pnl_pct=pnl_pct,
                                            pnl_sol=pnl_sol, sold_pct=left / pos["entry_tokens"] * 100 if pos["entry_tokens"] else 100,
                                            at=_at(ctx, pos["token"], fill), why=why or reason, position_id=pos["id"],
                                            last_bit=bool(hits)),
                    {"type": "post_mortem", "pos": pos, "reason": reason, "pnl_pct": pnl_pct, "age_h": age_h}
                    if pnl_sol < 0 else None)
        results.append({"position_id": pos["id"], "action": "exit", "reason": reason, "pnl_pct": round(pnl_pct, 2)})
    return results


def _take_profit(ctx: Context, pos: dict[str, Any], mark: FillEstimate, tokens_sold: float, rung: int, mult: float,
                 symbol: str, ladder: list[tuple[float, float]]) -> dict[str, Any]:
    """Sell one or more take-profit rungs; the rest of the position stays open."""
    conn = ctx.conn
    reason = f"take profit at {mult:.1f}x"
    if pos["mode"] == "paper":
        fill = _paper_sell(ctx, pos, tokens_sold, mark)
    else:
        try:
            fill = _live_sell(ctx, pos, tokens_sold)
        except LiveTradingBlocked as exc:
            journal_append(conn, "live_exit_blocked", {"position_id": pos["id"], "error": str(exc), "reason": reason},
                           ref=pos["token"], mode="live")
            return {"position_id": pos["id"], "action": "exit_blocked", "error": str(exc)}
    take_partial(conn, pos, fill=fill, rung=rung, reason=reason, symbol=symbol, mode=pos["mode"])
    share = fill.tokens_out / pos["entry_tokens"] if pos["entry_tokens"] else 0
    left_pct = max(0.0, (_left(pos) - fill.tokens_out) / pos["entry_tokens"] * 100) if pos["entry_tokens"] else 0
    nth = {1: "first", 2: "second", 3: "third"}.get(rung, f"#{rung}")
    nxt = f", next one at {ladder[rung][0]:g}x" if rung < len(ladder) else ""
    pnl_pct = (mult - 1) * 100
    # this post is the update for the gain so far: Grok says why the rest is still held, and the milestone
    # updates it already covers don't go out again separately
    s, key = ctx.settings, f"milestones:{pos['id']}"
    if s.x_update_step_pct > 0:
        done = {s.x_update_step_pct * k for k in range(1, int(pnl_pct // s.x_update_step_pct) + 1)}
        kv_set(conn, key, sorted(set(kv_get(conn, key, []) or []) | done))
    _post_later(ctx, "take_profit", dict(mode=pos["mode"], symbol=symbol, token=pos["token"], sold_pct=share * 100,
                                         at=_at(ctx, pos["token"], fill),
                                         why=f"{nth} target hit at {mult:.1f}x, stop moved to entry{nxt}",
                                         left_pct=left_pct, realized_sol=fill.quote_in - pos["entry_quote"] * share,
                                         position_id=pos["id"], rung=rung),
                {"type": "thesis", "pos": pos, "pnl_pct": pnl_pct,
                 "mc": _market_cap(ctx, pos["token"], fill.spot_price or fill.avg_price),
                 "note": f"just sold {share * 100:.0f}% at {mult:.1f}x; say why the other {left_pct:.0f}% is still "
                         "worth holding, under 120 characters"} if left_pct > 0 else None)
    return {"position_id": pos["id"], "action": "take_profit", "rung": rung, "multiple": round(mult, 2),
            "sold_sol": round(fill.quote_in, 6)}


def _milestone_update(ctx: Context, pos: dict[str, Any], pnl_pct: float, fill: FillEstimate, hits: int = 0) -> None:
    """Post once per milestone crossed (every +x_update_step_pct, and the down marks), under the entry
    post. If several are crossed at once, only the furthest one is posted."""
    s, conn = ctx.settings, ctx.conn
    key = f"milestones:{pos['id']}"
    hit = set(kv_get(conn, key, []) or [])
    ups = [s.x_update_step_pct * k for k in range(1, int(pnl_pct // s.x_update_step_pct) + 1)] \
        if s.x_update_step_pct > 0 and pnl_pct > 0 else []
    downs = [m for m in s.x_update_down_pct if pnl_pct <= m]
    crossed = [m for m in ups + downs if m not in hit]
    if not crossed:
        return
    kv_set(conn, key, sorted(hit | set(crossed)))
    m = max(crossed, key=abs)
    symbol = _symbol(ctx, pos["token"])
    mc = _market_cap(ctx, pos["token"], fill.spot_price)
    _post_later(ctx, "position_update", dict(mode=pos["mode"], symbol=symbol, token=pos["token"], pnl_pct=pnl_pct,
                                             price=f"{fill.avg_price:.3g} {QUOTE_SYMBOL}", milestone=m,
                                             plan=_plan(s, hits), position_id=pos["id"], mc=mc),
                {"type": "thesis", "pos": pos, "pnl_pct": pnl_pct, "mc": mc} if m > 0 else None)


def _post_later(ctx: Context, draft: str, kwargs: dict[str, Any], grok: dict[str, Any] | None) -> None:
    """The exit loop never waits on Grok: it journals the post to write (which compose function, its fields, and
    what Grok should add), and the posting loop writes it within seconds (post_jobs_once)."""
    journal_append(ctx.conn, "post_job", {"draft": draft, "kwargs": kwargs, "grok": grok}, ref=kwargs.get("token"))


def post_jobs_once(ctx: Context) -> int:
    """Write the posts the exit loop left: ask Grok for the post-mortem or the updated thesis, then queue the post.
    Posts carry fixed keys, so a job that runs twice still posts once."""
    conn = ctx.conn
    last = kv_get(conn, "post_jobs_cursor") or 0
    jobs = rows(conn, "SELECT id, payload FROM journal WHERE kind='post_job' AND id>? ORDER BY id", (last,))
    for j in jobs:
        p = json.loads(j["payload"]) if isinstance(j["payload"], str) else j["payload"]
        kw, m = p["kwargs"], p.get("grok") or {}
        try:
            if m.get("type") == "post_mortem":
                kw.update(_post_mortem(ctx, m["pos"], m["reason"], m["pnl_pct"], m["age_h"], kw["symbol"]))
            elif m.get("type") == "thesis":
                kw["thesis"] = _updated_thesis(ctx, m["pos"], kw["symbol"], m["pnl_pct"], m["mc"], note=m.get("note", ""))
        except Exception as exc:  # Grok's words are extra: the post still goes out without them
            log.warning("grok text for a %s post failed: %s", p["draft"], exc)
        try:
            enqueue(conn, getattr(compose, p["draft"])(**kw))
        except Exception as exc:
            log.exception("could not write a %s post: %s", p["draft"], exc)
        kv_set(conn, "post_jobs_cursor", j["id"])
    return len(jobs)


def _updated_thesis(ctx: Context, pos: dict[str, Any], symbol: str, pnl_pct: float, mc: str, note: str = "") -> str:
    """Grok's fresh read on a coin that's running, for the milestone post. Journalled; '' if Grok can't answer,
    and the update still goes out without it."""
    conn = ctx.conn
    try:
        d = conn.execute("SELECT thesis, reasons, risks FROM decisions WHERE id=?", (pos["decision_id"],)).fetchone() \
            if pos.get("decision_id") else None
        held_h = (datetime.now(timezone.utc) - datetime.fromisoformat(pos["opened_at"])).total_seconds() / 3600
        r = ctx.brain.update_thesis({
            "symbol": symbol, "token": pos["token"], "pnl_pct": round(pnl_pct, 1), "held_hours": round(held_h, 1),
            "market_cap_now": mc or None, "entry_sol": pos["entry_quote"], "just_now": note or None,
            "original_decision": {"thesis": d["thesis"], "reasons": json.loads(d["reasons"] or "[]"),
                                  "risks": json.loads(d["risks"] or "[]")} if d else {},
            "x_attention_24h": kv_get(conn, f"x_attention:{pos['token']}"),
            "evidence": evidence_for_token(conn, pos["token"], 10), "lore": _lore(ctx, pos["token"])})
    except Exception as exc:  # the update post never waits on this
        log.warning("updated thesis unavailable for %s: %s", pos["token"], exc)
        return ""
    thesis = (r.decision.thesis if r.decision else "").strip()
    journal_append(conn, "thesis_update", {"position_id": pos["id"], "token": pos["token"], "symbol": symbol,
                                           "pnl_pct": round(pnl_pct, 1), "thesis": thesis, "grok_error": r.error},
                   ref=pos["token"])
    return thesis


def weekly_scorecard(ctx: Context, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    if now.weekday() != 0:
        return False
    week = now.strftime("%G-W%V")
    mode = ctx.settings.trading_mode.value
    since = (now - timedelta(days=7)).isoformat()
    closed = rows(ctx.conn, "SELECT p.*, t.symbol FROM positions p LEFT JOIN tokens t ON t.mint=p.token "
                            "WHERE p.status='closed' AND p.mode=? AND p.closed_at>=?", (mode, since))
    if not closed:
        return False
    wins = [c for c in closed if (c["pnl_quote"] or 0) > 0]
    best = max(closed, key=lambda c: c["pnl_quote"] or 0)
    worst = min(closed, key=lambda c: c["pnl_quote"] or 0)
    fmt = lambda c: f"${c['symbol'] or '?'} {((c['exit_quote'] or 0) / c['entry_quote'] - 1) * 100:+.0f}%"  # noqa: E731
    own = ""
    if ctx.settings.agent_token_address:
        own = f"Disclosure: my own token is {ctx.settings.agent_token_address}; treasury {ctx.settings.treasury_address or 'n/a'}."
    _, created = enqueue(ctx.conn, compose.weekly_scorecard(
        week=week, mode=mode, trades=len(closed), wins=len(wins), losses=len(closed) - len(wins),
        pnl_quote=sum(c["pnl_quote"] or 0 for c in closed), quote_symbol=QUOTE_SYMBOL, best=fmt(best), worst=fmt(worst),
        own_token_line=own))
    return created
