"""Process loops. Each worker is its own OS process so that a failure or
outage in one (e.g. X down) cannot stop or corrupt another (e.g. trading).
"""
from __future__ import annotations

import logging
import signal
import subprocess
import sys
import time
from typing import Callable

from ..context import Context, build
from ..db import journal_append
from ..social.outbox import recover_interrupted, send_pending
from ..strategy.engine import assess_candidates, decide_and_act, manage_positions, weekly_scorecard
from .indexer import index_once
from .research import research_once

log = logging.getLogger("ct_agent")
_stop = False


def _handle_stop(*_: object) -> None:
    global _stop
    _stop = True


def trader_once(ctx: Context) -> dict:
    exits = manage_positions(ctx)
    killed, why = ctx.risk.kill_active()
    decisions = []
    if not killed:
        for a in assess_candidates(ctx):
            decisions.append(decide_and_act(ctx, a))
    weekly_scorecard(ctx)
    return {"exits": exits, "decisions": decisions, "killed": why if killed else None}


def social_once(ctx: Context) -> dict:
    res = send_pending(ctx.conn, ctx.x, enabled=ctx.settings.x_posting_enabled, daily_cap=ctx.settings.x_daily_post_cap)
    return {"sent": res}


WORKERS: dict[str, tuple[Callable[[Context], dict], float]] = {
    "indexer": (index_once, 10.0),
    "research": (research_once, 300.0),
    "trader": (trader_once, 60.0),
    "social": (social_once, 60.0),
}


def run_worker(name: str, *, once: bool = False) -> None:
    fn, interval = WORKERS[name]
    ctx = build()
    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)
    if name == "social":
        n = recover_interrupted(ctx.conn)
        if n:
            log.warning("%d posts were interrupted mid-send and are marked unknown; check X before retrying", n)
    log.info("worker %s started (interval %.0fs, mode %s)", name, interval, ctx.settings.trading_mode.value)
    failures = 0
    while not _stop:
        started = time.time()
        try:
            result = fn(ctx)
            failures = 0
            log.info("%s: %s", name, _brief(result))
        except Exception as exc:
            failures += 1
            log.exception("%s cycle failed (%d in a row)", name, failures)
            try:
                journal_append(ctx.conn, "worker_error", {"worker": name, "error": repr(exc)[:500]})
            except Exception:
                pass
        if once:
            break
        backoff = min(600, interval * (2 ** min(failures, 5))) if failures else interval
        while not _stop and time.time() - started < backoff:
            time.sleep(0.5)


def _brief(result: dict) -> str:
    return ", ".join(f"{k}={len(v) if isinstance(v, list) else v}" for k, v in result.items())


def run_all() -> None:
    """Start every worker as a separate process and supervise them."""
    procs: dict[str, subprocess.Popen] = {}

    def start(n: str) -> None:
        procs[n] = subprocess.Popen([sys.executable, "-m", "ct_agent.cli", "run", n])

    for n in WORKERS:
        start(n)
    procs["api"] = subprocess.Popen([sys.executable, "-m", "ct_agent.cli", "api"])
    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)
    try:
        while not _stop:
            for n, p in list(procs.items()):
                if p.poll() is not None:
                    log.warning("process %s exited with %s; restarting in 5s", n, p.returncode)
                    time.sleep(5)
                    if n == "api":
                        procs[n] = subprocess.Popen([sys.executable, "-m", "ct_agent.cli", "api"])
                    else:
                        start(n)
            time.sleep(2)
    finally:
        for p in procs.values():
            p.terminate()
        for p in procs.values():
            try:
                p.wait(timeout=15)
            except subprocess.TimeoutExpired:
                p.kill()
