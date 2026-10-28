"""A read-only check on the live trading bot, for a scheduled job: is the server up, are its connections working,
is the journal intact, can the wallet still trade, has the day's loss limit been hit, is a trade stuck open.

Only public reads: the server's /health and /positions, and the wallet's balance from a Solana RPC. No keys.

    python scripts/watch_trading.py --api https://<your server> --wallet <trading wallet address>

Prints one line per finding, then a summary line starting OK or ALERT. Exits 1 on any ALERT.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone

import httpx

NEEDED = ("jupiter", "grok", "x_post", "live_trading_ready")  # what live trading leans on


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--api", required=True)
    p.add_argument("--wallet", required=True)
    p.add_argument("--rpc", default="https://api.mainnet-beta.solana.com")
    p.add_argument("--min-sol", type=float, default=0.021, help="below this the wallet can't open a trade")
    p.add_argument("--daily-loss-sol", type=float, default=0.02)
    p.add_argument("--max-hold-hours", type=float, default=24)
    a = p.parse_args(argv)
    api = a.api.rstrip("/")
    alerts, notes = [], []
    c = httpx.Client(timeout=20)

    try:
        h = c.get(api + "/health").raise_for_status().json()
    except Exception as exc:
        print(f"ALERT server unreachable: {type(exc).__name__}")
        return 1
    if h.get("kill_switch", {}).get("on"):
        notes.append(f"paused by the kill switch ({h['kill_switch'].get('reason', '')})")
    if not h.get("journal", {}).get("chain_intact"):
        alerts.append(f"journal is broken at entry {h['journal'].get('first_bad_id')}")
    down = [k for k in NEEDED if not h.get("integrations", {}).get(k)]
    if down:
        alerts.append("not connected: " + ", ".join(down))
    if h.get("mode") != "live":
        notes.append(f"mode is {h.get('mode')}, not live")

    try:
        r = c.post(a.rpc, json={"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [a.wallet]}).json()
        bal = r["result"]["value"] / 1e9
        (alerts if bal < a.min_sol else notes).append(
            f"wallet {bal:.4f} SOL" + (f", below {a.min_sol} so it can't open a trade" if bal < a.min_sol else ""))
    except Exception as exc:
        notes.append(f"couldn't read the wallet balance: {type(exc).__name__}")

    try:
        pos = c.get(api + "/positions", params={"mode": "live"}).raise_for_status().json()
    except Exception as exc:
        pos = []
        notes.append(f"couldn't read positions: {type(exc).__name__}")
    now = datetime.now(timezone.utc)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    when = lambda t: datetime.fromisoformat(t.replace("Z", "+00:00"))
    open_ = [x for x in pos if x["status"] == "open"]
    closed_today = [x for x in pos if x["status"] == "closed" and x.get("closed_at") and when(x["closed_at"]) >= day]
    pnl = sum(x.get("pnl_quote") or 0 for x in closed_today)
    notes.append(f"{len(open_)} open, {len(closed_today)} closed today, today's P&L {pnl:+.4f} SOL")
    if pnl <= -a.daily_loss_sol:
        alerts.append(f"daily loss limit hit ({pnl:+.4f} SOL): no more buys until tomorrow (UTC)")
    for x in open_:
        if now - when(x["opened_at"]) > timedelta(hours=a.max_hold_hours + 1):
            alerts.append(f"${x.get('symbol') or x['token'][:6]} open since {x['opened_at'][:16]}, past its "
                          f"{a.max_hold_hours:g}h limit: the sell may be failing")

    for line in alerts:
        print("ALERT " + line)
    for line in notes:
        print("  " + line)
    print(("ALERT " if alerts else "OK ") + f"trading bot checked {now:%Y-%m-%d %H:%M} UTC")
    return 1 if alerts else 0


if __name__ == "__main__":
    sys.exit(main())
