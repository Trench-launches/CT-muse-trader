"""Calls from the smart-money scanner (its Supabase table `alert_performance`), Solana only.

The scanner is VIGIL's only source of coins. A call only becomes a candidate once the coin has run
+scanner_trigger_pct from the call price (USD, DexScreener), within scanner_window_hours of the call.
A coin is tradeable when it is a real token mint, nobody can still mint or freeze it (and no Token-2022
extension can take or tax holders' coins), and Jupiter can route a small SOL swap into it. Anything else
is skipped and says why, in plain words. Mints are base58 and case-sensitive: never lowercased.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from ..chain import token as tok
from ..chain.keys import SOL_MINT, is_pubkey
from ..chain.rpc import RpcError
from ..context import Context
from ..db import kv_get, kv_set, rows, tx, utcnow
from ..execution.jupiter import JupiterError
from ..market import tokens
from ..units import lamports

log = logging.getLogger(__name__)
PROBE_SOL = 0.01  # the small buy Jupiter must be able to route for a coin to count as tradeable
FIELDS = ("id,token_address,token_symbol,alert_price,alert_mcap,alert_timestamp,alert_score,alert_tier,"
          "alert_entry_phase,alert_signals,alert_liquidity,alert_volume,alert_age_days,alert_sm_wallets,"
          "alert_sm_netflow,alert_whale_flow,alert_sm_holders,alert_sm_held_usd,alert_supply_ctl_pct,source")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def fetch_new(ctx: Context) -> int:
    """Copy calls we haven't seen yet. The first run starts from calls still inside the window."""
    s, conn = ctx.settings, ctx.conn
    if not (s.supabase_url and s.supabase_key):
        return 0
    last = kv_get(conn, "scanner_cursor")
    q = f"chain=eq.solana&order=id.asc&limit=200&select={FIELDS}"
    if last is None:
        since = int((_now() - timedelta(hours=s.scanner_window_hours)).timestamp() * 1000)
        q += f"&alert_timestamp=gte.{since}"
    else:
        q += f"&id=gt.{int(last)}"
    r = ctx.web.get(f"{s.supabase_url.rstrip('/')}/rest/v1/alert_performance?{q}", timeout=20,
                    headers={"apikey": s.supabase_key, "Authorization": f"Bearer {s.supabase_key}"})
    r.raise_for_status()
    calls = r.json()
    with tx(conn):
        for c in calls:
            conn.execute("INSERT OR IGNORE INTO scanner_calls(id,token,symbol,called_at,call_price_usd,call_mcap_usd,"
                         "data,status,first_seen) VALUES(?,?,?,?,?,?,?,?,?)",
                         (int(c["id"]), c["token_address"], c.get("token_symbol"),
                          datetime.fromtimestamp(c["alert_timestamp"] / 1000, timezone.utc).isoformat(timespec="seconds"),
                          c.get("alert_price"), c.get("alert_mcap"), json.dumps(c), "watching", utcnow()))
        if calls:
            kv_set(conn, "scanner_cursor", max(int(c["id"]) for c in calls))
    if last is None and not calls:  # nothing in the window yet: start after the newest call, not from zero
        r = ctx.web.get(f"{s.supabase_url.rstrip('/')}/rest/v1/alert_performance?select=id&chain=eq.solana&order=id.desc&limit=1",
                        timeout=20, headers={"apikey": s.supabase_key, "Authorization": f"Bearer {s.supabase_key}"})
        r.raise_for_status()
        newest = r.json()
        kv_set(conn, "scanner_cursor", int(newest[0]["id"]) if newest else 0)
    return len(calls)


def fetch_fibbot(ctx: Context) -> int:
    """Fibbot's Solana smart-money calls (its table `smart_money_calls`, source 'smart_money' only; its
    Nansen alerts and fib alerts are ignored). Most are the same calls as the main scanner's; what Fibbot adds
    is re-calls: a coin called again later, which starts a fresh call with its own price and window. A Fibbot
    call within 15 minutes of a call VIGIL already has for that coin is the same call and is skipped."""
    s, conn = ctx.settings, ctx.conn
    if not (s.fibbot_supabase_url and s.fibbot_supabase_key):
        return 0
    last = kv_get(conn, "fibbot_cursor") or (_now() - timedelta(hours=s.scanner_window_hours)).isoformat()
    r = ctx.web.get(f"{s.fibbot_supabase_url.rstrip('/')}/rest/v1/smart_money_calls", timeout=20,
                    params={"chain": "eq.solana", "source": "eq.smart_money", "called_at": f"gt.{last}",
                            "order": "called_at.asc", "limit": "200",
                            "select": "id,token_address,symbol,called_at,call_price,call_mc,source"},
                    headers={"apikey": s.fibbot_supabase_key, "Authorization": f"Bearer {s.fibbot_supabase_key}"})
    r.raise_for_status()
    calls, added = r.json(), 0
    with tx(conn):
        for c in calls:
            at = datetime.fromisoformat(c["called_at"].replace("Z", "+00:00")).astimezone(timezone.utc)
            near = conn.execute("SELECT 1 FROM scanner_calls WHERE token=? AND called_at BETWEEN ? AND ?",
                                (c["token_address"], (at - timedelta(minutes=15)).isoformat(timespec="seconds"),
                                 (at + timedelta(minutes=15)).isoformat(timespec="seconds"))).fetchone()
            if near:
                continue
            cid = 1_000_000_000 + int(c["id"].replace("-", "")[:7], 16)  # Fibbot ids are uuids; keep them apart
            data = {"id": cid, "token_address": c["token_address"], "token_symbol": c["symbol"],
                    "alert_price": c["call_price"], "alert_mcap": c["call_mc"], "source": "fibbot_smart_money",
                    "fibbot_id": c["id"]}
            cur = conn.execute("INSERT OR IGNORE INTO scanner_calls(id,token,symbol,called_at,call_price_usd,call_mcap_usd,"
                               "data,status,first_seen) VALUES(?,?,?,?,?,?,?,?,?)",
                               (cid, c["token_address"], c["symbol"], at.isoformat(timespec="seconds"), c["call_price"],
                                c["call_mc"], json.dumps(data), "watching", utcnow()))
            added += cur.rowcount
        if calls:
            kv_set(conn, "fibbot_cursor", calls[-1]["called_at"])
    return added


def _pool_for(ctx: Context, call: dict[str, Any]) -> tuple[str | None, str | None]:
    """(pool id, None) for a tradeable coin, else (None, why it's skipped). The pool id is the coin's deepest
    SOL pool, or "jupiter" when Jupiter routes it but no pool is listed yet. (None, None) means the check
    couldn't run right now (RPC or Jupiter down): it is tried again next cycle."""
    s = ctx.settings
    token = call["token"]
    if s.agent_token_address and token == s.agent_token_address:
        return None, "our own token"
    if token in set(s.skip_tokens):
        return None, "a token we're tied to"
    if not is_pubkey(token) or token == SOL_MINT:
        return None, "not a Solana token address"
    try:
        info = tok.mint_info(ctx.rpc, token)
    except RpcError as exc:
        log.warning("mint check for %s failed: %s", token, exc)
        return None, None
    if not info.exists:
        return None, "not a Solana token mint"
    unsafe = tok.safety_blockers(info, s.max_transfer_fee_bps)
    if unsafe:
        return None, "unsafe coin: " + "; ".join(unsafe)
    try:
        ctx.jupiter.order(SOL_MINT, token, lamports(PROBE_SOL))
    except JupiterError as exc:
        if str(exc).startswith("no route"):
            return None, "Jupiter can't route a swap into it yet"
        log.warning("jupiter check for %s failed: %s", token, exc)
        return None, None
    pool = None
    try:
        pool = ctx.market.pool_stats(token)
    except Exception as exc:  # market data is extra: Jupiter's route is what makes it tradeable
        log.warning("pool stats for %s unavailable: %s", token, exc)
    try:
        tokens.ensure(ctx, token, info=info, pool=pool)
    except Exception as exc:
        log.warning("token cache for %s not filled: %s", token, exc)
    return (pool.pool_id if pool and pool.pool_id else "jupiter"), None


def _prices(ctx: Context, calls: list[dict[str, Any]]) -> dict[str, float]:
    """USD price of each coin (DexScreener, its deepest pair)."""
    try:
        return ctx.market.prices_usd([c["token"] for c in calls if c.get("pool_id")])
    except Exception as exc:  # a missed cycle just waits for the next
        log.warning("scanner prices unavailable: %s", exc)
        return {}


def refresh(ctx: Context) -> list[dict[str, Any]]:
    """Update every open call and return the ones that have made the move (the candidates)."""
    s, conn = ctx.settings, ctx.conn
    cutoff = (_now() - timedelta(hours=s.scanner_window_hours)).isoformat(timespec="seconds")
    with tx(conn):
        conn.execute("UPDATE scanner_calls SET status='expired' WHERE status='watching' AND called_at<?", (cutoff,))
        conn.execute("UPDATE scanner_calls SET status='bought' WHERE status='watching' AND token IN "
                     "(SELECT token FROM positions WHERE status='open')")
    open_calls = rows(conn, "SELECT * FROM scanner_calls WHERE status='watching' ORDER BY id")
    for c in open_calls:
        if c.get("pool_id") is None:
            pid, why = _pool_for(ctx, c)
            if pid is None and why is None:
                continue  # couldn't check right now: next cycle
            with tx(conn):
                if why:
                    conn.execute("UPDATE scanner_calls SET status='skipped', note=? WHERE id=?", (why, c["id"]))
                else:
                    conn.execute("UPDATE scanner_calls SET pool_id=? WHERE id=?", (pid, c["id"]))
            c["pool_id"], c["status"] = pid, "skipped" if why else c["status"]
    live = [c for c in open_calls if c["status"] == "watching" and c.get("pool_id")]
    prices = _prices(ctx, live)
    ready = []
    for c in live:
        px = prices.get(c["token"])
        if not px or not c.get("call_price_usd"):
            continue
        move = (px / c["call_price_usd"] - 1) * 100
        with tx(conn):
            conn.execute("UPDATE scanner_calls SET last_price_usd=?, last_move_pct=?, peak_move_pct=MAX(COALESCE("
                         "peak_move_pct, ?), ?), checked_at=?, triggered_at=CASE WHEN ?>=? THEN COALESCE(triggered_at, ?) "
                         "ELSE triggered_at END WHERE id=?",
                         (px, round(move, 2), move, move, utcnow(), move, s.scanner_trigger_pct, utcnow(), c["id"]))
        if move >= s.scanner_trigger_pct:  # still up at least the trigger now, not just once
            c.update(last_price_usd=px, last_move_pct=round(move, 2))
            ready.append(c)
    if s.scanner_buy_on_call:  # every tradeable call is bought as it lands, priced or not
        seen = {c["id"] for c in ready}
        ready += [c for c in live if c["id"] not in seen]
    return ready


def to_analyse(conn: Any, window_hours: float, limit: int = 3) -> list[dict[str, Any]]:
    """Calls Grok hasn't looked at yet, oldest first: every Solana call gets a read and a post, including
    ones VIGIL can't trade (unsafe, or Jupiter can't route it). Only our own token is left out."""
    cutoff = (_now() - timedelta(hours=window_hours)).isoformat(timespec="seconds")
    return rows(conn, "SELECT * FROM scanner_calls WHERE decision_id IS NULL AND called_at>=? AND "
                      "((status IN ('watching','bought') AND pool_id IS NOT NULL) OR "
                      "(status='skipped' AND COALESCE(note,'')!='our own token')) ORDER BY id LIMIT ?", (cutoff, limit))


def mark_analysed(conn: Any, call_id: int, decision_id: int) -> None:
    with tx(conn):
        conn.execute("UPDATE scanner_calls SET decision_id=? WHERE id=?", (decision_id, call_id))


def mark_bought(conn: Any, token: str) -> None:
    with tx(conn):
        conn.execute("UPDATE scanner_calls SET status='bought' WHERE token=? AND status='watching'", (token,))


def call_facts(call: dict[str, Any], trigger_pct: float) -> dict[str, Any]:
    """What the scanner saw at the call, plus the move since, for Grok's briefing."""
    d = json.loads(call.get("data") or "{}")
    try:
        signals = json.loads(d.get("alert_signals") or "{}")
    except (TypeError, ValueError):
        signals = {}
    return {
        "called_at": call["called_at"], "scanner": d.get("source"), "tier": d.get("alert_tier"),
        "score": d.get("alert_score"), "entry_phase": d.get("alert_entry_phase"),
        "market_cap_at_call_usd": call.get("call_mcap_usd"), "liquidity_at_call_usd": d.get("alert_liquidity"),
        "volume_24h_at_call_usd": d.get("alert_volume"), "coin_age_days_at_call": d.get("alert_age_days"),
        "smart_money": {"wallets_traded_24h": d.get("alert_sm_wallets"), "net_flow_usd": d.get("alert_sm_netflow"),
                        "whale_flow_usd": d.get("alert_whale_flow"), "holders": d.get("alert_sm_holders"),
                        "held_usd": d.get("alert_sm_held_usd"), "supply_controlled_pct": d.get("alert_supply_ctl_pct")},
        "signals": signals,
        "move_since_call_pct": call.get("last_move_pct"), "peak_move_since_call_pct": call.get("peak_move_pct"),
        "rule": (f"VIGIL buys this call automatically, by rule, if the coin gets to +{trigger_pct:g}% from the call "
                 "price. Your job here is the analysis: what the coin is, why smart money may be in, what could go "
                 "wrong. Your action does not decide the trade."),
    }
