"""Paper broker: timestamped simulated trades.

Every entry and exit is journalled with paper=true and onchain=false, so no
post or dashboard can present a paper trade as an onchain one.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from ..db import journal_append, tx, utcnow
from .pricing import FillEstimate


def open_position(conn: sqlite3.Connection, *, token: str, symbol: str, decision_id: int | None,
                  quote_symbol: str, fill: FillEstimate, stage: str, meta: dict[str, Any] | None = None) -> int:
    with tx(conn):
        cur = conn.execute(
            """INSERT INTO positions(token,mode,status,decision_id,quote_symbol,opened_at,entry_quote,
                   entry_tokens,entry_price) VALUES(?,?,?,?,?,?,?,?,?)""",
            (token, "paper", "open", decision_id, quote_symbol, utcnow(), fill.quote_in, fill.tokens_out,
             fill.avg_price),
        )
        pid = int(cur.lastrowid)
        journal_append(conn, "paper_fill", {
            "paper": True, "onchain": False, "position_id": pid, "side": "buy", "token": token, "symbol": symbol,
            "stage": stage, "quote_in": fill.quote_in, "quote_symbol": quote_symbol, "tokens_out": fill.tokens_out,
            "avg_price": fill.avg_price, "spot_price": fill.spot_price, "price_impact_bps": fill.price_impact_bps,
            "fee_and_tax": fill.fee_and_tax_quote, "pricing": fill.method, "decision_id": decision_id,
            **(meta or {}),
        }, ref=token, mode="paper", in_tx=True)
    return pid


def close_position(conn: sqlite3.Connection, position: dict[str, Any], *, fill: FillEstimate, reason: str,
                   symbol: str = "") -> float:
    pnl = fill.quote_in - position["entry_quote"]
    with tx(conn):
        conn.execute("UPDATE positions SET status='closed', closed_at=?, exit_quote=?, exit_price=?, pnl_quote=?, "
                     "exit_reason=? WHERE id=? AND status='open'",
                     (utcnow(), fill.quote_in, fill.avg_price, pnl, reason, position["id"]))
        journal_append(conn, "paper_exit", {
            "paper": True, "onchain": False, "position_id": position["id"], "side": "sell",
            "token": position["token"], "symbol": symbol, "quote_out": fill.quote_in,
            "quote_symbol": position["quote_symbol"], "tokens_in": fill.tokens_out, "avg_price": fill.avg_price,
            "entry_price": position["entry_price"], "pnl_quote": pnl,
            "pnl_pct": (pnl / position["entry_quote"] * 100) if position["entry_quote"] else 0.0,
            "reason": reason, "pricing": fill.method,
        }, ref=position["token"], mode="paper", in_tx=True)
    return pnl
