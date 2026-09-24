"""Chain indexer process: launches, phase changes, curve trades, progress.

Scales to Robinhood Chain's pace (~0.1s blocks, thousands of launches a
day) by pulling all CurveBuy/CurveSell events chain-wide per range and
refreshing only tokens that traded, using batched RPC calls.
"""
from __future__ import annotations

import logging

from ..chain.abi import ZERO_ADDRESS, checksum, decode_output, topic_to_address
from ..chain.pons import CURVE_BUY, CURVE_SELL
from ..context import Context
from ..db import kv_get, kv_set, tx, utcnow

log = logging.getLogger(__name__)
SNAPSHOT_MIN_PROGRESS = 0.5


def measure_block_time(ctx: Context, head: int, span: int = 36_000) -> float:
    cached = kv_get(ctx.conn, "block_time_s")
    if cached and cached.get("head", 0) > head - 200_000:
        return float(cached["value"])
    lo = max(1, head - span)
    bt = (ctx.rpc.block_timestamp(head) - ctx.rpc.block_timestamp(lo)) / (head - lo)
    bt = bt if bt > 0 else 0.1
    kv_set(ctx.conn, "block_time_s", {"value": bt, "head": head})
    return bt


def index_once(ctx: Context, *, max_blocks: int | None = None) -> dict[str, int]:
    s, conn, pons = ctx.settings, ctx.conn, ctx.pons
    head = ctx.rpc.block_number()
    bt = measure_block_time(ctx, head)
    stats = {"head": head, "launches": 0, "trades": 0, "refreshed": 0, "phase_changes": 0}

    # 1) launches + phase events (sparse, big chunks)
    lc = kv_get(conn, "launch_cursor")
    start = lc + 1 if lc is not None else (s.pons_start_block or max(0, head - int(s.launch_backfill_hours * 3600 / bt)))
    end = head if max_blocks is None else min(head, start + max_blocks)
    if start <= end:
        pons.log_chunk = s.launch_log_chunk_blocks
        try:
            launches = pons.launches_between(start, end)
            phases = pons.phase_events(start, end)
        finally:
            pons.log_chunk = s.log_chunk_blocks
        with tx(conn):
            for e in launches:
                cur = conn.execute(
                    """INSERT OR IGNORE INTO launches(token,curve,deployer,pair_token,launch_config_id,
                       graduation_threshold,block_number,tx_hash,first_seen,quote_symbol,quote_decimals)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (e.token, e.curve, e.deployer, e.pair_token, e.launch_config_id, str(e.graduation_threshold),
                     e.block_number, e.tx_hash, utcnow(),
                     "ETH" if e.pair_token.lower() == ZERO_ADDRESS else None,
                     18 if e.pair_token.lower() == ZERO_ADDRESS else None))
                stats["launches"] += cur.rowcount
            for token, phase, _blk in phases:
                cur = conn.execute("UPDATE launches SET last_phase=?, last_progress=CASE WHEN ?>=1 THEN 1.0 "
                                   "ELSE last_progress END WHERE token=? AND last_phase<?",
                                   (int(phase), int(phase), token, int(phase)))
                stats["phase_changes"] += cur.rowcount
            kv_set(conn, "launch_cursor", end)

    # 2) curve trades chain-wide, filtered to known curves
    tc = kv_get(conn, "trade_cursor")
    t_start = tc + 1 if tc is not None else max(0, head - int(s.trade_lookback_s / bt))
    t_end = head if max_blocks is None else min(head, t_start + max_blocks)
    touched: dict[str, int] = {}
    if t_start <= t_end:
        logs = ctx.rpc.get_logs_chunked(address=None, topics=[[CURVE_BUY, CURVE_SELL]], from_block=t_start,
                                        to_block=t_end, chunk=s.log_chunk_blocks)
        curve_map = {r["curve"].lower(): r["token"] for r in conn.execute("SELECT curve, token FROM launches")}
        with tx(conn):
            for lg in logs:
                token = curve_map.get(lg["address"].lower())
                if not token:
                    continue  # launched before our backfill window
                side = "buy" if lg["topics"][0].lower() == CURVE_BUY else "sell"
                a, b, fee, tax = decode_output(["uint256"] * 4, lg["data"])
                q, t = (a, b) if side == "buy" else (b, a)
                blk = int(lg["blockNumber"], 16)
                cur = conn.execute("INSERT OR IGNORE INTO curve_trades VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                                   (lg["transactionHash"], int(lg["logIndex"], 16), token, checksum(lg["address"]),
                                    blk, side, topic_to_address(lg["topics"][1]), str(q), str(t), str(fee),
                                    str(tax)))
                stats["trades"] += cur.rowcount
                touched[token] = max(touched.get(token, 0), blk)
            kv_set(conn, "trade_cursor", t_end)

    # 3) refresh progress for curve-phase tokens that traded (one batch)
    if touched:
        rows_ = conn.execute(f"SELECT token, curve, graduation_threshold FROM launches WHERE last_phase=0 AND token IN "
                             f"({','.join('?' * len(touched))})", list(touched)).fetchall()
        reserves = pons.real_quote_reserves([r["curve"] for r in rows_])
        with tx(conn):
            for r in rows_:
                rq = reserves.get(r["curve"])
                if rq is None:
                    continue
                thr = int(r["graduation_threshold"])
                prog = rq / thr if thr else 0.0
                conn.execute("UPDATE launches SET last_progress=?, last_trade_block=?, last_snapshot_at=? WHERE token=?",
                             (prog, touched[r["token"]], utcnow(), r["token"]))
                if prog >= SNAPSHOT_MIN_PROGRESS:
                    conn.execute("INSERT INTO token_snapshots(token,ts,block_number,phase,real_quote_reserve,"
                                 "graduation_threshold,progress) VALUES(?,?,?,?,?,?,?)",
                                 (r["token"], utcnow(), head, 0, str(rq), str(thr), prog))
                stats["refreshed"] += 1
        with tx(conn):
            for token, blk in touched.items():
                conn.execute("UPDATE launches SET last_trade_block=? WHERE token=? AND last_phase!=0", (blk, token))

    # 4) retention
    keep_from = head - int(s.trade_retention_hours * 3600 / bt)
    conn.execute("DELETE FROM curve_trades WHERE block_number < ?", (keep_from,))
    kv_set(conn, "indexer_heartbeat", {"head": head, "at": utcnow()})  # the site's 'synced Ns ago'
    return stats
