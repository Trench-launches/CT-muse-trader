"""Command line entry point.

  ctagent init-db                  create/upgrade the database
  ctagent check                    test every configured connection (read-only)
  ctagent run <worker> [--once]    indexer | research | trader | social
  ctagent run-all                  every worker + API, each in its own process
  ctagent api [--port 8000]        HTTP API
  ctagent kill on|off "reason"     stop switch
  ctagent verify-journal           recompute the journal hash chain
  ctagent twitch-subscribe <callback_url> [--bot-user-id ID]
  ctagent x-login [--no-browser]   authorize posting as @MooseOnMuse (one time)
  ctagent report [--hours 24]      what happened after each call, by variable
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from .config import get_settings


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    p = argparse.ArgumentParser(prog="ctagent")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db")
    sub.add_parser("check")
    r = sub.add_parser("run")
    r.add_argument("worker", choices=["indexer", "research", "trader", "social"])
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
    t = sub.add_parser("twitch-subscribe")
    t.add_argument("callback")
    t.add_argument("--bot-user-id")
    args = p.parse_args(argv)
    s = get_settings()

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
    elif args.cmd == "twitch-subscribe":
        from .sources.twitch import TwitchApi
        api = TwitchApi(s.twitch_client_id, s.twitch_client_secret)
        for ch in s.twitch_channels:
            print(ch, api.subscribe_channel(ch, args.bot_user_id, args.callback, s.twitch_eventsub_secret))
    return 0


def _x_login(s, *, open_browser: bool) -> int:
    """One-time consent as @MooseOnMuse. Saves the refresh token to .env (never printed)."""
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
    print(f"{r['calls_tracked']} calls tracked. {r['note']}
")
    head = f"{'':28}" + "".join(f"{c:>14}" for c in cols)
    print(head)
    print(f"{'ALL':28}" + "".join(f"{fmt(r['overall'].get(c)):>14}" for c in cols))
    for var, buckets in r["by_variable"].items():
        print(f"
{var}")
        for b, v in buckets.items():
            print(f"  {b:26}" + "".join(f"{fmt(v.get(c)):>14}" for c in cols))
    if r["best_calls"]:
        print("
best calls:", ", ".join(f"${c['symbol']} {c['action']} {c['peak_pct']:+.0f}%" for c in r["best_calls"]))


def _check() -> int:
    """Read-only connectivity check. Never posts, never trades."""
    from .context import build
    ctx = build()
    s = ctx.settings
    report: dict[str, str] = {}
    try:
        cid = ctx.rpc.chain_id()
        report["rpc"] = f"ok chain {cid}" + ("" if cid == s.chain_id else f" (EXPECTED {s.chain_id})")
        report["pons_factory"] = "ok" if ctx.rpc.get_code(s.pons_v2_factory) not in ("0x", "") else "NO CODE"
        report["pons_hook"] = ctx.pons.meme_hook()
        bps, secs = ctx.pons.snipe_tax()
        report["snipe_tax"] = f"{bps / 100:.0f}% decaying over {secs}s"
        report["launch_fee_eth"] = str(ctx.pons.launch_fee() / 1e18)
    except Exception as exc:
        report["rpc"] = f"FAILED {exc}"
    try:
        report["eth_usd"] = str(ctx.market.eth_usd())
    except Exception as exc:
        report["eth_usd"] = f"FAILED {exc}"
    if s.muse_api_key:
        r = ctx.brain.decide({"token": "healthcheck", "note": "Reply with action pass. This is a connectivity test."})
        report["muse"] = "ok" if r.decision else f"FAILED {r.error}"
    else:
        report["muse"] = "not configured"
    if s.x_bearer_token:
        try:
            ctx.x.mention_counts('"robinhood chain"')
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
    killed, why = ctx.risk.kill_active()
    report["kill_switch"] = why if killed else "off"
    print(json.dumps(report, indent=2))
    return 0 if not report["rpc"].startswith("FAILED") else 1


if __name__ == "__main__":
    sys.exit(main())
