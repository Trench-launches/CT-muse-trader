"""Decision engine: screen -> brief Muse -> hard gates -> act -> post.

Muse can downgrade anything, but can never upgrade past a hard gate:
screen blockers, onchain verification, evidence requirements, risk limits,
slippage bounds and the kill switch all sit after Muse in the pipeline.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from ..chain.abi import ZERO_ADDRESS
from ..chain.pons import LaunchRecord, Phase
from ..brain.muse import MuseDecision, MuseResult
from ..context import Context
from ..db import journal_append, journal_recent, kv_get, kv_set, rows, tx, utcnow
from ..execution.live import LiveTradingBlocked
from ..execution.paper import close_position, open_position, take_partial
from ..execution.pricing import FillEstimate, curve_buy, curve_sell, pool_estimate
from ..social import compose
from ..social.outbox import enqueue
from ..sources.store import evidence_for_token, independent_origins
from .outcomes import record_call, spot_price
from .screen import Candidate, activity_for, screen_almost_bonded, screen_migrated

log = logging.getLogger(__name__)
ETH = 10**18
BUY_GRADE_CONFIDENCE = ("verified", "first_party", "reported")
NO_EVIDENCE = "no source mentions this token yet, so there is nothing to act on"


@dataclass
class Assessed:
    cand: Candidate
    launch: dict[str, Any]
    record: LaunchRecord
    pool: Any = None          # PoolStats for migrated
    eth_usd: float | None = None


# --- screening --------------------------------------------------------------

def _meta(ctx: Context, launch: dict[str, Any]) -> dict[str, Any]:
    if not launch.get("symbol"):
        m = ctx.pons.token_meta(launch["token"])
        ctx.conn.execute("UPDATE launches SET name=?, symbol=?, decimals=? WHERE token=?",
                         (m.name, m.symbol, m.decimals, launch["token"]))
        launch.update(name=m.name, symbol=m.symbol, decimals=m.decimals)
    if launch.get("logo_url") is None:
        logo = ctx.pons.token_logo(launch["token"])[:500]
        ctx.conn.execute("UPDATE launches SET logo_url=? WHERE token=?", (logo, launch["token"]))
        launch["logo_url"] = logo
    if not launch.get("quote_symbol"):
        # Many Pons launches are priced in Robinhood stock tokens or stablecoins
        # (GLD, NVDA, META, USDG...), not ETH. Record which, once per token.
        pair = (launch.get("pair_token") or "").lower()
        if pair == ZERO_ADDRESS:
            qs, qd = "ETH", 18
        else:
            q = ctx.pons.token_meta(launch["pair_token"])
            qs, qd = (q.symbol or "ERC20"), q.decimals
        ctx.conn.execute("UPDATE launches SET quote_symbol=?, quote_decimals=? WHERE token=?", (qs, qd, launch["token"]))
        launch.update(quote_symbol=qs, quote_decimals=qd)
    return launch


def assess_candidates(ctx: Context) -> list[Assessed]:
    s, conn = ctx.settings, ctx.conn
    head = ctx.rpc.block_number()
    bt = float((kv_get(conn, "block_time_s") or {}).get("value", 0.1))
    try:
        snipe_s = ctx.pons.snipe_tax()[1]
    except Exception:
        snipe_s = s.snipe_window_fallback_s
    out: list[Assessed] = []

    near = rows(conn, "SELECT * FROM launches WHERE last_phase=0 AND last_progress>=? "
                      "ORDER BY last_progress DESC LIMIT 40", (s.almost_bonded_ratio,))
    for l in near:
        ok, why, rec = ctx.pons.verify_token(l["token"])
        if not ok or rec is None:
            continue
        if rec.phase != Phase.NOT_GRADUATED:
            conn.execute("UPDATE launches SET last_phase=? WHERE token=?", (int(rec.phase), l["token"]))
            continue
        l = _meta(ctx, l)
        st = ctx.pons.curve_state(rec.curve)
        conn.execute("UPDATE launches SET last_progress=?, fee_bps=?, creator_tax_bps=? WHERE token=?",
                     (st.progress, st.fee_bps, st.creator_tax_bps, l["token"]))
        act = activity_for(conn, l["token"], l["deployer"], head, bt, s.activity_windows_s)
        c = screen_almost_bonded(s, token=l["token"], progress=st.progress, phase=int(rec.phase),
                                 launch_age_s=(head - l["block_number"]) * bt, snipe_window_s=snipe_s,
                                 fee_bps=st.fee_bps, creator_tax_bps=st.creator_tax_bps, activity=act,
                                 quote_symbol=l["quote_symbol"])
        out.append(Assessed(c, l, rec))

    migrated = rows(conn, "SELECT * FROM launches WHERE last_phase=2 ORDER BY COALESCE(last_trade_block, block_number) "
                          "DESC LIMIT 25")
    eth_usd = ctx.market.eth_usd() if migrated else None
    for l in migrated:
        ok, why, rec = ctx.pons.verify_token(l["token"])
        if not ok or rec is None or rec.phase != Phase.POOL_CREATED:
            continue
        l = _meta(ctx, l)
        pid = l.get("pool_id") or ctx.pons.genuine_pool_id(rec)
        if not l.get("pool_id"):
            conn.execute("UPDATE launches SET pool_id=?, pool_fee=?, tick_spacing=? WHERE token=?",
                         (pid, rec.pool_fee, rec.tick_spacing, l["token"]))
        pool = ctx.market.genuine_pool_stats(l["token"], pid)
        if pool:
            conn.execute("INSERT INTO market_snapshots(token,ts,source,pool_address,price_usd,volume_h1_usd,"
                         "volume_h24_usd,liquidity_usd,txns_h1) VALUES(?,?,?,?,?,?,?,?,?)",
                         (l["token"], utcnow(), pool.source, pid, pool.price_usd, pool.volume_h1_usd,
                          pool.volume_h24_usd, pool.liquidity_usd, pool.txns_h1))
        fee = l.get("fee_bps") or 100
        c = screen_migrated(s, token=l["token"], phase=int(rec.phase), pool_found=pool is not None,
                            volume_h1_usd=pool.volume_h1_usd if pool else None,
                            liquidity_usd=pool.liquidity_usd if pool else None,
                            volume_h24_usd=pool.volume_h24_usd if pool else None,
                            fee_bps=fee, creator_tax_bps=rec.creator_tax_bps, activity=None,
                            quote_symbol=l["quote_symbol"])
        out.append(Assessed(c, l, rec, pool, eth_usd))

    eligible = sorted((a for a in out if a.cand.eligible), key=lambda a: a.cand.score, reverse=True)
    kv_set(conn, "candidates", [{"token": a.cand.token, "symbol": a.launch.get("symbol"), "stage": a.cand.stage,
                                 "score": a.cand.score, "progress": a.cand.progress} for a in eligible[:20]])
    journal_append(conn, "screen_cycle", {"assessed": len(out), "eligible": len(eligible),
                                          "top": [a.cand.token for a in eligible[:5]]})
    return eligible[: s.candidates_per_cycle]


# --- briefing pack ----------------------------------------------------------

def build_pack(ctx: Context, a: Assessed) -> dict[str, Any]:
    conn, s = ctx.conn, ctx.settings
    ev = evidence_for_token(conn, a.cand.token)
    for e in ev:
        e["independent_origins"] = independent_origins(conn, e["claim_key"]) if e.get("claim_key") else 1
    attention = kv_get(conn, f"x_attention:{a.cand.token.lower()}")
    past = [{"ts": j["ts"], "kind": j["kind"], "payload": j["payload"]}
            for j in journal_recent(conn, ref=a.cand.token, limit=10)]
    mistakes = rows(conn, "SELECT token, exit_reason, pnl_quote, closed_at FROM positions WHERE status='closed' "
                          "AND pnl_quote<0 ORDER BY id DESC LIMIT 5")
    quote = a.launch.get("quote_symbol") or "ERC20"
    pack: dict[str, Any] = {
        "token": a.cand.token, "symbol": a.launch.get("symbol"), "name": a.launch.get("name"),
        "chain": "Robinhood Chain (4663)", "launchpad": "Pons V2", "stage": a.cand.stage,
        "phase": Phase(a.cand.phase).name, "curve_progress": a.cand.progress, "quote_asset": quote,
        "fees": {"curve_fee_bps": a.launch.get("fee_bps"), "creator_tax_bps": a.record.creator_tax_bps},
        "screen": {"score": a.cand.score, "flags": a.cand.flags, "metrics": a.cand.metrics},
        "x_attention_24h": attention,
        "evidence": [{k: e.get(k) for k in ("id", "kind", "author", "channel", "published_at", "confidence",
                                             "origin_url", "excerpt", "independent_origins")} for e in ev],
        "past_journal_for_token": past,
        "recent_losing_trades": mistakes,
        "limits": {"max_position_eth": s.max_position_eth, "mode": s.trading_mode.value},
        "rules": {"verified_source_required": s.min_independent_sources_for_buy > 0},
        "fees_and_size": _fee_facts(ctx, a),
        "holders": holder_facts(ctx, a.launch),
        "lore": _lore(ctx, a.cand.token),
    }
    if a.pool:
        pack["pool_indicative"] = {"source": a.pool.source, "volume_h1_usd": a.pool.volume_h1_usd,
                                   "volume_h24_usd": a.pool.volume_h24_usd, "liquidity_usd": a.pool.liquidity_usd,
                                   "price_native": a.pool.price_native, "eth_usd": a.eth_usd,
                                   "note": "aggregator data, indicative only"}
    return pack


# --- decide & act -----------------------------------------------------------

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


def _market_cap_usd(ctx: Context, token: str, price_eth: float | None) -> float | None:
    """Market cap in USD: price x total supply x ETH/USD. None when any part is unknown."""
    try:
        if not price_eth:
            return None
        row = ctx.conn.execute("SELECT decimals, quote_symbol FROM launches WHERE token=?", (token,)).fetchone()
        if row and row["quote_symbol"] and row["quote_symbol"] != "ETH":
            return None  # priced in a stock token or stablecoin: the price isn't in ETH
        supply = kv_get(ctx.conn, f"supply:{token.lower()}")
        if supply is None:
            dec = row["decimals"] if row and row["decimals"] is not None else 18
            supply = ctx.pons.total_supply(token) / 10 ** dec
            kv_set(ctx.conn, f"supply:{token.lower()}", supply)
        eth = ctx.market.eth_usd()
        return price_eth * supply * eth if eth else None
    except Exception as exc:  # a missing figure must never block a post or a trade
        log.warning("market cap unavailable for %s: %s", token, exc)
        return None


def _market_cap(ctx: Context, token: str, price_eth: float | None) -> str:
    """Live market cap for a post, e.g. '$42.1K'. '' when unknown, so a post never shows an invented figure."""
    mc = _market_cap_usd(ctx, token, price_eth)
    return compose.usd_compact(mc) if mc else ""


def fees_paid_eth(ctx: Context, launch: dict[str, Any]) -> float | None:
    """Trading fees + creator tax traders have paid on the coin's curve over its whole life, in ETH.
    Read from the chain's own trade events, cached, and extended from the last block read."""
    try:
        token = launch["token"]
        key = f"fees_paid:{token.lower()}"
        st = kv_get(ctx.conn, key) or {"to": int(launch["block_number"]) - 1, "wei": "0", "trades": 0}
        head = ctx.rpc.block_number()
        if head > st["to"]:
            fee, n = ctx.pons.curve_fees(launch["curve"], st["to"] + 1, head, ctx.settings.launch_log_chunk_blocks)
            st = {"to": head, "wei": str(int(st["wei"]) + fee), "trades": st["trades"] + n}
            kv_set(ctx.conn, key, st)
        return int(st["wei"]) / ETH
    except Exception as exc:  # never let a failed read block a decision; the gate just can't apply
        log.warning("fees paid unavailable for %s: %s", launch.get("token"), exc)
        return None


DEAD = {"0x0000000000000000000000000000000000000000", "0x000000000000000000000000000000000000dead"}


def holder_facts(ctx: Context, launch: dict[str, Any]) -> dict[str, Any] | None:
    """Who holds the supply: rebuilt from the token's own Transfer events (cached, extended from the last
    block read). The curve, the pool manager, the hook, burn addresses and the token itself don't count."""
    try:
        token = launch["token"]
        key = f"holders:{token.lower()}"
        st = kv_get(ctx.conn, key) or {"to": int(launch["block_number"]) - 1, "bal": {}}
        head = ctx.rpc.block_number()
        if head > st["to"]:
            bal = {a: int(v) for a, v in st["bal"].items()}
            for frm, to, amt in ctx.pons.token_transfers(token, st["to"] + 1, head, ctx.settings.launch_log_chunk_blocks):
                bal[frm] = bal.get(frm, 0) - amt
                bal[to] = bal.get(to, 0) + amt
            st = {"to": head, "bal": {a: str(v) for a, v in bal.items() if v > 0}}
            kv_set(ctx.conn, key, st)
        supply = ctx.pons.total_supply(token)
        if not supply:
            return None
        skip = set(DEAD) | {token.lower(), str(launch.get("curve") or "").lower()}
        for getter in (ctx.pons.pool_manager, ctx.pons.meme_hook):
            try:
                skip.add(getter().lower())
            except Exception:
                pass
        wallets = sorted((int(v) for a, v in st["bal"].items() if a not in skip), reverse=True)
        dev = int(st["bal"].get(str(launch.get("deployer") or "").lower(), 0))
        return {"holders": len(wallets), "top10_share": round(sum(wallets[:10]) / supply, 4),
                "top_wallet_share": round((wallets[0] if wallets else 0) / supply, 4),
                "deployer_share": round(dev / supply, 4)}
    except Exception as exc:  # a failed read never blocks a decision; the gate just can't apply
        log.warning("holders unavailable for %s: %s", launch.get("token"), exc)
        return None


def _fee_facts(ctx: Context, a: Assessed) -> dict[str, Any]:
    """Market cap and lifetime fees paid, for Muse and for the thin-chart gate."""
    try:
        price = spot_price(ctx, a.cand.token)[0]
    except Exception:
        price = None
    fees = fees_paid_eth(ctx, a.launch)
    if fees is not None and a.pool and a.pool.volume_h24_usd and a.eth_usd:
        # graduated: add what the pool took in the last 24h (its lifetime isn't readable cheaply)
        rate = (a.cand.metrics.get("trade_fee_bps") or 0) / 10_000
        fees += a.pool.volume_h24_usd * rate / a.eth_usd
    mc = _market_cap_usd(ctx, a.cand.token, price)
    return {"market_cap_usd": round(mc) if mc else None, "trading_fees_paid_eth": round(fees, 4) if fees is not None else None}


def _lore(ctx: Context, token: str) -> dict[str, Any]:
    """The coin's own story and links; never lets a failed look-up block a decision."""
    from ..sources.lore import get_lore
    try:
        return get_lore(ctx, token)
    except Exception as exc:
        log.warning("lore unavailable for %s: %s", token, exc)
        return {}


def _awaiting_research(ctx: Context, token: str) -> bool:
    """True while a newly shortlisted token has no X research yet, for up to
    research_wait_min. Deciding before the research exists would turn every
    new token into an automatic 'no evidence' pass."""
    if not ctx.settings.x_bearer_token or kv_get(ctx.conn, f"x_attention:{token.lower()}"):
        return False
    key = f"first_shortlisted:{token.lower()}"
    first = kv_get(ctx.conn, key)
    if not first:
        kv_set(ctx.conn, key, utcnow())
        return True
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(first)).total_seconds() / 60
    return age < ctx.settings.research_wait_min


def _recent_decision(ctx: Context, token: str) -> bool:
    since = (datetime.now(timezone.utc) - timedelta(minutes=ctx.settings.decision_cooldown_min)).isoformat()
    return ctx.conn.execute("SELECT 1 FROM decisions WHERE token=? AND ts>=? LIMIT 1", (token, since)).fetchone() \
        is not None


def _fill_for(ctx: Context, a: Assessed, size_eth: float) -> FillEstimate:
    fee = a.launch.get("fee_bps") or 100
    if a.cand.stage == "almost_bonded":
        q, t = ctx.pons.curve_reserves(a.record.curve)
        return curve_buy(int(size_eth * ETH), q, t, fee, a.record.creator_tax_bps)
    p = a.pool
    return pool_estimate(size_eth, p.price_native, p.liquidity_usd, a.eth_usd or 0, fee + a.record.creator_tax_bps)


def decide_and_act(ctx: Context, a: Assessed) -> dict[str, Any]:
    conn, s = ctx.conn, ctx.settings
    token, symbol = a.cand.token, a.launch.get("symbol") or "?"
    if _recent_decision(ctx, token):
        return {"token": token, "skipped": "cooldown"}
    if _awaiting_research(ctx, token):
        return {"token": token, "skipped": "waiting for X research"}
    pack = build_pack(ctx, a)
    no_source_ok = not s.min_independent_sources_for_buy
    if pack["evidence"] or (no_source_ok and a.cand.eligible):
        res = ctx.brain.decide(pack)
    else:
        # Nothing Muse could buy: no source when one is required, or no source and the screen already
        # blocks it. Recording the pass without asking Muse saves a paid call per token.
        why = "screen blockers: " + "; ".join(a.cand.blockers) if no_source_ok else NO_EVIDENCE
        res = MuseResult(MuseDecision(action="pass", confidence=1.0, thesis="", reasons=[why], risks=[],
                                      evidence_ids=[], underfollowed=None, post_angle="", public_line=""),
                         raw="", model="rule", prompt_hash="")
    gates: list[str] = []
    if res.decision is None:
        action, conf, thesis, reasons, risks, used = "watch", 0.0, "", [res.error or "no decision"], [], []
    else:
        d = res.decision
        action, conf, thesis, reasons, risks, used = d.action, d.confidence, d.thesis, d.reasons, d.risks, d.evidence_ids

    if action == "buy":
        provided = {e["id"]: e for e in pack["evidence"]}
        cited = [provided[i] for i in used if i in provided]
        if len(cited) != len(used):
            gates.append("Muse cited evidence ids that were not provided")
        # Rumors and chat can motivate a watch, never a buy on their own.
        independent = {e.get("origin_url") for e in cited if e["confidence"] in BUY_GRADE_CONFIDENCE}
        if len(independent) < s.min_independent_sources_for_buy:
            gates.append(f"needs {s.min_independent_sources_for_buy} verified/first-party/reported source(s)")
        if not a.cand.eligible:
            gates.append("screen blockers: " + "; ".join(a.cand.blockers))
        if not risks:
            gates.append("no concrete risk named")
        fz = pack.get("fees_and_size") or {}
        mc, paid = fz.get("market_cap_usd"), fz.get("trading_fees_paid_eth")
        # both guards fail safe: if the chain can't be read right now, don't buy (a later cycle can)
        if s.min_fees_paid_eth > 0 and paid is None:
            gates.append("couldn't check the fees traders have paid")
        elif mc and paid is not None and mc > s.fee_check_mc_usd and paid < s.min_fees_paid_eth:
            gates.append(f"only {paid:.3f} ETH of trading fees paid at a ${mc:,.0f} market cap: thin or botted chart")
        hs = pack.get("holders") or {}
        if not hs:
            gates.append("couldn't check who holds the supply")
        if hs.get("deployer_share", 0) > s.max_deployer_hold_share:
            gates.append(f"the deployer still holds {hs['deployer_share']:.0%} of the supply")
        if hs.get("top10_share", 0) > s.max_top10_hold_share:
            gates.append(f"the 10 biggest wallets hold {hs['top10_share']:.0%} of the supply")
        verdict = ctx.risk.check_entry(token=token, quote_symbol=a.launch.get("quote_symbol") or "", mode=s.trading_mode.value)
        gates.extend(verdict.reasons)
        fill = None
        if not gates:
            fill = _fill_for(ctx, a, verdict.size_eth)
            if fill.price_impact_bps > s.max_slippage_bps:
                gates.append(f"price impact {fill.price_impact_bps} bps > {s.max_slippage_bps}")
        if gates:
            action = "watch"

    with tx(conn):
        cur = conn.execute("INSERT INTO decisions(ts,token,stage,action,confidence,thesis,reasons,risks,source_ids,"
                           "model,raw) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                           (utcnow(), token, a.cand.stage, action, conf, thesis, json.dumps(reasons + gates),
                            json.dumps(risks), json.dumps(used), res.model, res.raw))
        did = int(cur.lastrowid)
        journal_append(conn, "decision", {"decision_id": did, "token": token, "symbol": symbol, "stage": a.cand.stage,
                                          "action": action, "confidence": conf, "gates": gates, "reasons": reasons,
                                          "narrative": res.decision.narrative.model_dump() if res.decision else None,
                                          "risks": risks, "evidence_ids": used, "prompt_hash": res.prompt_hash,
                                          "muse_error": res.error}, ref=token, in_tx=True)

    try:
        record_call(ctx, a, decision_id=did, action=action, confidence=conf, pack=pack, gates=gates, model=res.model)
    except Exception as exc:  # tracking is for analysis; it must never stop a trade
        log.warning("could not record call outcome for %s: %s", token, exc)
    out: dict[str, Any] = {"token": token, "decision_id": did, "action": action, "gates": gates}
    if action == "buy":
        out.update(_enter(ctx, a, did, fill, verdict.size_eth, used, risks, pack))
    elif action == "watch" and conf >= 0.6 and res.decision:
        skip, parent = _thread(conn, token, "watch")
        if not skip:
            pb = res.decision.post
            d = compose.watch_note(symbol=symbol, token=token, decision_id=did, narrative=pb.narrative,
                                   confirmation=pb.confirmation, watching=pb.watching, trigger=pb.trigger,
                                   line=res.decision.public_line or thesis)
            d.reply_to = parent
            enqueue(conn, d)
    elif action == "pass" and res.decision and pack["evidence"] and conf >= 0.7:
        skip, parent = _thread(conn, token, "pass")
        if not skip:
            pb = res.decision.post
            d = compose.pass_note(symbol=symbol, token=token, decision_id=did, narrative=pb.narrative,
                                  concern=pb.concern, reconsider_if=pb.reconsider_if,
                                  line=res.decision.public_line or (reasons or ["not convinced"])[0])
            d.reply_to = parent
            enqueue(conn, d)
    return out


def _enter(ctx: Context, a: Assessed, did: int, fill: FillEstimate, size_eth: float, used: list[int],
           risks: list[str], pack: dict[str, Any]) -> dict[str, Any]:
    s, conn = ctx.settings, ctx.conn
    token, symbol = a.cand.token, a.launch.get("symbol") or "?"
    mode = s.trading_mode.value
    if mode == "live":
        try:
            if a.cand.stage != "almost_bonded":
                ctx.live.swap_on_pool()
            lf = ctx.live.buy_on_curve(token=token, curve=a.record.curve, quote_in_wei=int(size_eth * ETH),
                                       fee_bps=a.launch.get("fee_bps") or 100, tax_bps=a.record.creator_tax_bps)
        except LiveTradingBlocked as exc:
            journal_append(conn, "live_entry_blocked", {"token": token, "error": str(exc), "decision_id": did},
                           ref=token, mode="live")
            return {"entered": False, "error": str(exc)}
        tokens = lf.token_amount / ETH
        spent = lf.quote_amount / ETH
        with tx(conn):
            cur = conn.execute("INSERT INTO positions(token,mode,status,decision_id,quote_symbol,opened_at,entry_quote,"
                               "entry_tokens,entry_price) VALUES(?,?,?,?,?,?,?,?,?)",
                               (token, "live", "open", did, "ETH", utcnow(), spent, tokens, spent / tokens if tokens else 0))
            pid = int(cur.lastrowid)
        entry_price = spent / tokens if tokens else 0
    else:
        pid = open_position(conn, token=token, symbol=symbol, decision_id=did, quote_symbol="ETH", fill=fill,
                            stage=a.cand.stage, meta={"evidence_ids": used})
        entry_price, spent = fill.avg_price, fill.quote_in
    cited = [e for e in pack["evidence"] if e["id"] in used]
    catalyst = _describe_source(cited[0]) if cited else "broad onchain buying"
    # Only claim momentum that was actually measured (curve tokens); graduated
    # tokens are screened on pool volume instead, so say that.
    windows = a.cand.metrics.get("windows") or []
    if windows and a.cand.metrics.get("accelerating"):
        momentum = "buys accelerating over " + "/".join(f"{w['seconds'] // 60}m" for w in windows)
    elif a.cand.metrics.get("volume_heat"):
        momentum = f"1h volume at {a.cand.metrics['volume_heat']:.1f}x its daily average"
    else:
        momentum = ""
    d = compose.trade_entry(mode=mode, symbol=symbol, token=token, size=f"{spent:.4f} ETH",
                            entry=f"{entry_price:.3g} ETH", catalyst=catalyst, progress=a.cand.progress,
                            momentum=momentum, risk=risks[0], position_id=pid,
                            own_token=token.lower() == s.agent_token_address.lower(),
                            mc=_market_cap(ctx, token, fill.spot_price if mode == "paper" else entry_price))
    d.reply_to = _thread(conn, token, "trade_entry")[1]  # under an earlier watch/pass, if any
    enqueue(conn, d)
    return {"entered": True, "position_id": pid}


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


def _mark(ctx: Context, pos: dict[str, Any], tokens: float) -> tuple[FillEstimate | None, str]:
    """What selling `tokens` of this position would return right now."""
    rec = ctx.pons.launch_record(pos["token"])
    fee = 100
    if rec.phase == Phase.NOT_GRADUATED:
        q, t = ctx.pons.curve_reserves(rec.curve)
        st = ctx.pons.curve_state(rec.curve)
        return curve_sell(int(tokens * ETH), q, t, st.fee_bps, st.creator_tax_bps), "curve"
    if rec.phase == Phase.POOL_CREATED:
        pool = ctx.market.genuine_pool_stats(pos["token"], ctx.pons.genuine_pool_id(rec))
        eth = ctx.market.eth_usd()
        if pool and pool.price_native and pool.liquidity_usd and eth:
            return pool_estimate(tokens, pool.price_native, pool.liquidity_usd, eth,
                                 fee + rec.creator_tax_bps, buy=False), "pool"
        return None, "pool data unavailable"
    return None, f"phase {rec.phase.name}"


def _live_sell(ctx: Context, pos: dict[str, Any], venue: str, tokens: float) -> FillEstimate:
    """Sell `tokens` onchain. Raises LiveTradingBlocked if it can't."""
    if venue != "curve":
        ctx.live.swap_on_pool()
    rec = ctx.pons.launch_record(pos["token"])
    st = ctx.pons.curve_state(rec.curve)
    lf = ctx.live.sell_on_curve(token=pos["token"], curve=rec.curve, tokens_in_wei=int(tokens * ETH),
                                fee_bps=st.fee_bps, tax_bps=st.creator_tax_bps)
    return FillEstimate(lf.quote_amount / ETH, lf.token_amount / ETH,
                        (lf.quote_amount / lf.token_amount) if lf.token_amount else 0, 0, 0, 0, "onchain")


def _at(ctx: Context, token: str, fill: FillEstimate) -> str:
    """Where a sell happened, for the post: market cap if known, else the fill price."""
    mc = _market_cap(ctx, token, fill.spot_price or fill.avg_price)
    return f"{mc} MC" if mc else f"{fill.avg_price:.3g} ETH"


def _post_mortem(ctx: Context, pos: dict[str, Any], reason: str, pnl_pct: float, age_h: float,
                 symbol: str) -> dict[str, str]:
    """Muse's honest read on a losing trade for the exit post, journalled so it isn't lost.
    Falls back to the original thesis alone if Muse can't answer."""
    conn = ctx.conn
    d = conn.execute("SELECT thesis, reasons, risks FROM decisions WHERE id=?", (pos["decision_id"],)).fetchone() \
        if pos.get("decision_id") else None
    buy = {"thesis": d["thesis"], "reasons": json.loads(d["reasons"] or "[]"), "risks": json.loads(d["risks"] or "[]")} if d else {}
    r = ctx.brain.review_loss({"symbol": symbol, "token": pos["token"], "entry_eth": pos["entry_quote"],
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
    first rung the stop moves to entry; whatever is left when the timer runs out is sold. (Muse's thesis
    review used to sell here too, but a fresh position always looks down by the round-trip fees, so it
    sold new trades within a minute.)"""
    s, conn = ctx.settings, ctx.conn
    ladder = sorted(s.take_profit_ladder)
    results = []
    for pos in ctx.risk.open_positions(s.trading_mode.value):
        left, hits = _left(pos), pos.get("tp_hits") or 0
        fill, venue = _mark(ctx, pos, left)
        if fill is None:
            journal_append(conn, "mark_failed", {"position_id": pos["id"], "why": venue}, ref=pos["token"])
            continue
        realized = pos.get("realized_quote") or 0.0
        value = realized + fill.quote_in  # the whole trade: sold so far plus the rest at market
        pnl_pct = (value / pos["entry_quote"] - 1) * 100 if pos["entry_quote"] else 0
        # price multiple on what's still held: what it sells for now per token vs what it cost per token
        cost = pos["entry_quote"] / pos["entry_tokens"] if pos["entry_tokens"] else 0
        mult = (fill.quote_in / left) / cost if left and cost else 0
        marks = (kv_get(conn, f"marks:{pos['id']}") or [])[-29:]  # recent marks for the site's sparkline
        kv_set(conn, f"marks:{pos['id']}", marks + [round(value, 8)])
        age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(pos["opened_at"])).total_seconds() / 3600
        crossed = [i for i, (m, _) in enumerate(ladder) if i >= hits and mult >= m]
        row = conn.execute("SELECT symbol FROM launches WHERE token=?", (pos["token"],)).fetchone()
        symbol = (row["symbol"] if row else None) or "?"
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
                results.append(_take_profit(ctx, pos, venue, sell, top + 1, mult, symbol, ladder))
                continue
        elif age_h >= s.max_hold_hours:  # the timer covers the whole trade, the runner included
            reason = f"max hold {s.max_hold_hours:.0f}h reached"
            why = f"{s.max_hold_hours:.0f}h passed" + ("" if hits else " without the move")
        if not reason:
            _milestone_update(ctx, pos, pnl_pct, fill, hits)
            results.append({"position_id": pos["id"], "pnl_pct": round(pnl_pct, 2), "action": "hold"})
            continue
        if pos["mode"] == "paper":
            close_position(conn, pos, fill=fill, reason=reason, symbol=symbol)
        else:
            try:
                fill = _live_sell(ctx, pos, venue, left)
                total = realized + fill.quote_in
                conn.execute("UPDATE positions SET status='closed', closed_at=?, exit_quote=?, exit_price=?, pnl_quote=?, "
                             "exit_reason=?, tokens_left=0 WHERE id=?",
                             (utcnow(), total, fill.avg_price, total - pos["entry_quote"], reason, pos["id"]))
            except LiveTradingBlocked as exc:
                journal_append(conn, "live_exit_blocked", {"position_id": pos["id"], "error": str(exc), "reason": reason},
                               ref=pos["token"], mode="live")
                results.append({"position_id": pos["id"], "action": "exit_blocked", "error": str(exc)})
                continue
        pnl_eth = realized + fill.quote_in - pos["entry_quote"]
        pnl_pct = pnl_eth / pos["entry_quote"] * 100 if pos["entry_quote"] else 0
        story = _post_mortem(ctx, pos, reason, pnl_pct, age_h, symbol) if pnl_eth < 0 else {}
        enqueue(conn, compose.trade_exit(mode=pos["mode"], symbol=symbol, token=pos["token"], pnl_pct=pnl_pct,
                                         pnl_eth=pnl_eth, sold_pct=left / pos["entry_tokens"] * 100 if pos["entry_tokens"] else 100,
                                         at=_at(ctx, pos["token"], fill), why=why or reason, position_id=pos["id"],
                                         last_bit=bool(hits), **story))
        results.append({"position_id": pos["id"], "action": "exit", "reason": reason, "pnl_pct": round(pnl_pct, 2)})
    return results


def _take_profit(ctx: Context, pos: dict[str, Any], venue: str, tokens: float, rung: int, mult: float, symbol: str,
                 ladder: list[tuple[float, float]]) -> dict[str, Any]:
    """Sell one or more take-profit rungs; the rest of the position stays open."""
    conn = ctx.conn
    reason = f"take profit at {mult:.1f}x"
    if pos["mode"] == "paper":
        fill, _ = _mark(ctx, pos, tokens)
        if fill is None:
            return {"position_id": pos["id"], "action": "take_profit_failed", "error": "no mark"}
    else:
        try:
            fill = _live_sell(ctx, pos, venue, tokens)
        except LiveTradingBlocked as exc:
            journal_append(conn, "live_exit_blocked", {"position_id": pos["id"], "error": str(exc), "reason": reason},
                           ref=pos["token"], mode="live")
            return {"position_id": pos["id"], "action": "exit_blocked", "error": str(exc)}
    take_partial(conn, pos, fill=fill, rung=rung, reason=reason, symbol=symbol, mode=pos["mode"])
    share = fill.tokens_out / pos["entry_tokens"] if pos["entry_tokens"] else 0
    left_pct = max(0.0, (_left(pos) - fill.tokens_out) / pos["entry_tokens"] * 100) if pos["entry_tokens"] else 0
    nth = {1: "first", 2: "second", 3: "third"}.get(rung, f"#{rung}")
    nxt = f", next one at {ladder[rung][0]:g}x" if rung < len(ladder) else ""
    enqueue(conn, compose.take_profit(mode=pos["mode"], symbol=symbol, token=pos["token"], sold_pct=share * 100,
                                      at=_at(ctx, pos["token"], fill),
                                      why=f"{nth} target hit at {mult:.1f}x, stop moved to entry{nxt}",
                                      left_pct=left_pct, realized_eth=fill.quote_in - pos["entry_quote"] * share,
                                      position_id=pos["id"], rung=rung))
    return {"position_id": pos["id"], "action": "take_profit", "rung": rung, "multiple": round(mult, 2),
            "sold_eth": round(fill.quote_in, 6)}


def _milestone_update(ctx: Context, pos: dict[str, Any], pnl_pct: float, fill: FillEstimate, hits: int = 0) -> None:
    """Post once per milestone crossed (every +30%, and -15%), under the entry
    post. If several are crossed at once, only the furthest one is posted."""
    s, conn = ctx.settings, ctx.conn
    key = f"milestones:{pos['id']}"
    hit = set(kv_get(conn, key, []) or [])
    ups = [s.x_update_step_pct * k for k in range(1, int(pnl_pct // s.x_update_step_pct) + 1)]         if s.x_update_step_pct > 0 and pnl_pct > 0 else []
    downs = [m for m in s.x_update_down_pct if pnl_pct <= m]
    crossed = [m for m in ups + downs if m not in hit]
    if not crossed:
        return
    kv_set(conn, key, sorted(hit | set(crossed)))
    m = max(crossed, key=abs)
    symbol = (conn.execute("SELECT symbol FROM launches WHERE token=?", (pos["token"],)).fetchone() or {"symbol": "?"})["symbol"]
    enqueue(conn, compose.position_update(mode=pos["mode"], symbol=symbol or "?", token=pos["token"], pnl_pct=pnl_pct,
                                          price=f"{fill.avg_price:.3g} ETH", milestone=m, plan=_plan(s, hits),
                                          position_id=pos["id"],
                                          mc=_market_cap(ctx, pos["token"], fill.spot_price)))


def weekly_scorecard(ctx: Context, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    if now.weekday() != 0:
        return False
    week = now.strftime("%G-W%V")
    mode = ctx.settings.trading_mode.value
    since = (now - timedelta(days=7)).isoformat()
    closed = rows(ctx.conn, "SELECT p.*, l.symbol FROM positions p LEFT JOIN launches l ON l.token=p.token "
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
        pnl_quote=sum(c["pnl_quote"] or 0 for c in closed), quote_symbol="ETH", best=fmt(best), worst=fmt(worst),
        own_token_line=own))
    return created
