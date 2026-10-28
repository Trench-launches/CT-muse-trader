"""Command line entry point.

  ctagent init-db                  create/upgrade the database
  ctagent check                    test every configured connection (read-only)
  ctagent run <worker> [--once]    caller | research | exits | trader | social | launchpad
  ctagent run-all                  every worker + API, each in its own process
  ctagent api [--port 8000]        HTTP API
  ctagent kill on|off "reason"     stop switch
  ctagent verify-journal           recompute the journal hash chain
  ctagent twitch-subscribe <callback_url> [--bot-user-id ID]
  ctagent x-login [--no-browser]   authorize posting as the bot's X account (one time)
  ctagent report [--hours 24]      what happened after each call, by variable
  ctagent wallet                   the trading wallet's address and SOL balance (never the key)
  ctagent live-test <mint> [--sol 0.01]    one tiny real buy then sell through Jupiter
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from .config import get_settings


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # httpx logs every request URL at INFO, and RPC URLs carry the provider key: keep those out of the logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    p = argparse.ArgumentParser(prog="ctagent")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db")
    sub.add_parser("check")
    r = sub.add_parser("run")
    from .workers.runner import WORKERS
    r.add_argument("worker", choices=list(WORKERS))  # every worker run-all starts, so the two lists never drift
    r.add_argument("--once", action="store_true")
    sub.add_parser("run-all")
    a = sub.add_parser("api")
    a.add_argument("--host", default="127.0.0.1")
    a.add_argument("--port", type=int, default=8000)
    k = sub.add_parser("kill")
    k.add_argument("state", choices=["on", "off"])
    k.add_argument("reason")
    sub.add_parser("verify-journal")
    rp = sub.add_parser("report")
    rp.add_argument("--hours", type=float)
    xl = sub.add_parser("x-login")
    xl.add_argument("--no-browser", action="store_true")
    sub.add_parser("wallet")
    lt = sub.add_parser("live-test")
    lt.add_argument("token")
    lt.add_argument("--sol", type=float, default=0.01)
    t = sub.add_parser("twitch-subscribe")
    t.add_argument("callback")
    t.add_argument("--bot-user-id")
    args = p.parse_args(argv)
    s = get_settings()
    if args.cmd in ("run", "run-all", "api", "live-test"):
        from .safety import CHECKED_ENV, UnsafeSettings, enforce
        try:
            enforce(s)
        except UnsafeSettings as exc:
            print(f"ctagent: {exc}", file=sys.stderr)
            return 2
        if args.cmd == "run-all":
            os.environ[CHECKED_ENV] = "1"  # the processes it starts inherit this and don't repeat the review

    if args.cmd == "init-db":
        from .db import open_db
        open_db(s.database_path).close()
        print(f"database ready at {s.database_path}")
    elif args.cmd == "check":
        return _check()
    elif args.cmd == "run":
        from .workers.runner import run_worker
        run_worker(args.worker, once=args.once)
    elif args.cmd == "run-all":
        from .workers.runner import run_all
        run_all()
    elif args.cmd == "api":
        import uvicorn
        from .api.app import create_app
        uvicorn.run(create_app(s), host=args.host, port=args.port)
    elif args.cmd == "kill":
        from .context import build
        ctx = build(s)
        ctx.risk.set_kill(args.state == "on", args.reason, "cli")
        print(f"kill switch {args.state}")
    elif args.cmd == "verify-journal":
        from .db import journal_verify, open_db
        ok, bad = journal_verify(open_db(s.database_path))
        print("journal intact" if ok else f"journal TAMPERED at id {bad}")
        return 0 if ok else 2
    elif args.cmd == "report":
        from .db import open_db
        from .strategy.outcomes import report
        _print_report(report(open_db(s.database_path), args.hours))
    elif args.cmd == "x-login":
        return _x_login(s, open_browser=not args.no_browser)
    elif args.cmd == "wallet":
        from .context import build
        ctx = build(s)
        if not ctx.live.wallet:
            print("no trading wallet key set (CT_TRADING_WALLET_PRIVATE_KEY)")
            return 1
        from .units import sol
        print(f"trading wallet {ctx.live.wallet}: {sol(ctx.rpc.get_balance(ctx.live.wallet)):.6f} SOL")
    elif args.cmd == "live-test":
        return _live_test(s, args.token, args.sol)
    elif args.cmd == "twitch-subscribe":
        from .sources.twitch import TwitchApi
        api = TwitchApi(s.twitch_client_id, s.twitch_client_secret)
        for ch in s.twitch_channels:
            print(ch, api.subscribe_channel(ch, args.bot_user_id, args.callback, s.twitch_eventsub_secret))
    return 0


def _live_test(s, token: str, size_sol: float) -> int:
    """Prove the live path with real money before switching the bot to live: buy `size_sol` of a coin through
    Jupiter, then sell everything bought. Uses every live gate except the mode switch."""
    from .config import TradingMode
    from .context import build
    from .execution.live import LiveTradingBlocked
    from .market import tokens
    from .units import from_base, lamports, sol
    if size_sol > 0.05:
        print("live-test is capped at 0.05 SOL")
        return 1
    ctx = build(s.model_copy(update={"trading_mode": TradingMode.LIVE, "live_execution_reviewed": True}))
    if not ctx.live.wallet:
        print("no valid trading wallet key set (CT_TRADING_WALLET_PRIVATE_KEY)")
        return 1
    row = tokens.ensure(ctx, token)
    if not row:
        print(f"{token} is not a Solana token mint")
        return 1
    dec = int(row["decimals"])
    bal = sol(ctx.rpc.get_balance(ctx.live.wallet))
    print(f"wallet {ctx.live.wallet}: {bal:.6f} SOL")
    try:
        buy = ctx.live.swap(token, "buy", lamports(size_sol))
        print(f"bought {from_base(buy.token_amount, dec):,.2f} ${row['symbol'] or '?'} for {sol(buy.quote_amount):.6f} SOL"
              f"  {s.explorer_url}/tx/{buy.tx_hash}")
        sell = ctx.live.swap(token, "sell", buy.token_amount)
        print(f"sold {from_base(sell.token_amount, dec):,.2f} for {sol(sell.quote_amount):.6f} SOL"
              f"  {s.explorer_url}/tx/{sell.tx_hash}")
    except LiveTradingBlocked as exc:
        print(f"blocked: {exc}")
        return 1
    after = sol(ctx.rpc.get_balance(ctx.live.wallet))
    print(f"round trip cost {bal - after:.6f} SOL incl. fees and token-account rent; wallet now {after:.6f} SOL")
    return 0


def _x_login(s, *, open_browser: bool) -> int:
    """One-time consent as the bot's X account. Saves the refresh token to .env (never printed)."""
    from pathlib import Path

    from .sources.x_oauth import XOAuth2, browser_login
    if not s.x_client_id:
        print("CT_X_CLIENT_ID is empty: add the OAuth 2.0 Client ID and Secret to .env first")
        return 1
    user, refresh = browser_login(XOAuth2(client_id=s.x_client_id, client_secret=s.x_client_secret),
                                  open_browser=open_browser)
    env = Path(".env")
    lines = env.read_text(encoding="utf-8").splitlines() if env.exists() else []
    lines = [ln for ln in lines if not ln.startswith("CT_X_REFRESH_TOKEN=")]
    lines.append("CT_X_REFRESH_TOKEN=" + refresh)
    env.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Connected as @{user}. Saved CT_X_REFRESH_TOKEN to .env; copy it to the server's variables.")
    return 0


def _print_report(r: dict) -> None:
    cols = ["n", "avg_15m", "avg_1h", "avg_4h", "avg_24h", "hit_rate_+25%", "dead_rate", "graduated"]
    fmt = lambda v: "-" if v is None else str(v)  # noqa: E731
    print(f"{r['calls_tracked']} calls tracked. {r['note']}\n")
    head = f"{'':28}" + "".join(f"{c:>14}" for c in cols)
    print(head)
    print(f"{'ALL':28}" + "".join(f"{fmt(r['overall'].get(c)):>14}" for c in cols))
    for var, buckets in r["by_variable"].items():
        print(f"\n{var}")
        for b, v in buckets.items():
            print(f"  {b:26}" + "".join(f"{fmt(v.get(c)):>14}" for c in cols))
    if r["best_calls"]:
        print("\nbest calls:", ", ".join(f"${c['symbol']} {c['action']} {c['peak_pct']:+.0f}%" for c in r["best_calls"]))


def _check() -> int:
    """Read-only connectivity check. Never posts, never trades."""
    from .chain.keys import MAINNET_GENESIS, SOL_MINT, USDC_MINT
    from .chain.rpc import redact
    from .context import build
    from .units import lamports
    ctx = build()
    s = ctx.settings
    report: dict[str, str] = {}
    try:
        g = ctx.rpc.genesis_hash()
        report["solana_rpc"] = (f"ok mainnet (slot {ctx.rpc.slot()})" if g == MAINNET_GENESIS
                                else f"NOT MAINNET (genesis {g})") + f" via {redact(s.solana_rpc_url)}"
    except Exception as exc:
        report["solana_rpc"] = f"FAILED {redact(str(exc))}"
    try:
        q = ctx.jupiter.order(SOL_MINT, USDC_MINT, lamports(0.01))
        report["jupiter"] = f"ok: 0.01 SOL -> {int(q['outAmount']) / 1e6:.4f} USDC" + \
            ("" if s.jupiter_api_key else " (no API key: slow lane)")
    except Exception as exc:
        report["jupiter"] = f"FAILED {exc}"
    try:
        report["sol_usd"] = str(ctx.market.sol_usd())
    except Exception as exc:
        report["sol_usd"] = f"FAILED {exc}"
    if s.grok_api_key:
        r = ctx.brain.decide({"token": "healthcheck", "note": "Reply with action pass. This is a connectivity test."})
        report["grok"] = "ok" if r.decision else f"FAILED {r.error}"
    else:
        report["grok"] = "not configured"
    if s.supabase_url and s.supabase_key:
        try:
            r = ctx.web.get(f"{s.supabase_url.rstrip('/')}/rest/v1/alert_performance", timeout=20,
                            params={"chain": "eq.solana", "select": "id,token_symbol,alert_timestamp",
                                    "order": "id.desc", "limit": "3"},
                            headers={"apikey": s.supabase_key, "Authorization": f"Bearer {s.supabase_key}"})
            r.raise_for_status()
            latest = r.json()
            report["scanner"] = "ok, latest Solana calls: " + (", ".join(
                f"${c.get('token_symbol') or '?'}" for c in latest) or "none yet")
        except Exception as exc:
            report["scanner"] = f"FAILED {exc}"
    else:
        report["scanner"] = "not configured"
    if s.x_bearer_token:
        try:
            ctx.x.mention_counts('"solana"')
            report["x_read"] = "ok"
        except Exception as exc:
            report["x_read"] = f"FAILED {exc}"
    else:
        report["x_read"] = "not configured"
    can_post = ctx.x.can_post
    report["x_post"] = "configured (enabled)" if can_post and s.x_posting_enabled else \
        ("configured (disabled)" if can_post else "not configured: run ctagent x-login")
    report["twitch"] = "configured" if s.twitch_client_id and s.twitch_eventsub_secret else "not configured"
    report["live_trading"] = "BLOCKED: " + "; ".join(ctx.live.blockers()) if ctx.live.blockers() else "READY"
    if ctx.live.wallet:
        report["trading_wallet"] = ctx.live.wallet
    killed, why = ctx.risk.kill_active()
    report["kill_switch"] = why if killed else "off"
    from .safety import review
    errors, warnings = review(s)
    report["safety"] = "; ".join([f"ERROR {e}" for e in errors] + [f"warning {w}" for w in warnings]) or "ok"
    print(json.dumps(report, indent=2))
    return 0 if report["solana_rpc"].startswith("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
