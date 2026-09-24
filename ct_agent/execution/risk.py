"""Hard limits and the stop switch. Muse can never loosen these.

Limits are in ETH, so only ETH-quoted launches can be sized. ERC-20
quoted launches are blocked until a priced conversion is added.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..config import Settings, TradingMode
from ..db import journal_append, kv_get, kv_set, rows


@dataclass
class RiskVerdict:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    size_eth: float = 0.0


class RiskManager:
    def __init__(self, settings: Settings, conn: sqlite3.Connection):
        self.s = settings
        self.conn = conn

    # --- stop switch --------------------------------------------------------

    def kill_active(self) -> tuple[bool, str]:
        if Path(self.s.kill_switch_file).exists():
            return True, f"kill file present: {self.s.kill_switch_file}"
        k = kv_get(self.conn, "kill_switch")
        if k and k.get("on"):
            return True, f"kill switch on: {k.get('reason')}"
        return False, ""

    def set_kill(self, on: bool, reason: str, actor: str) -> None:
        kv_set(self.conn, "kill_switch", {"on": on, "reason": reason, "actor": actor,
                                          "at": datetime.now(timezone.utc).isoformat()})
        journal_append(self.conn, "kill_switch", {"on": on, "reason": reason, "actor": actor})

    # --- exposure -----------------------------------------------------------

    def open_positions(self, mode: str) -> list[dict]:
        return rows(self.conn, "SELECT * FROM positions WHERE status='open' AND mode=?", (mode,))

    def exposure_eth(self, mode: str) -> float:
        return sum(p["entry_quote"] for p in self.open_positions(mode))

    def realized_today_eth(self, mode: str) -> float:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        r = self.conn.execute("SELECT COALESCE(SUM(pnl_quote),0) v FROM positions "
                              "WHERE mode=? AND status='closed' AND substr(closed_at,1,10)=?", (mode, today)).fetchone()
        return float(r["v"])

    def paper_cash_eth(self) -> float:
        r = self.conn.execute("SELECT COALESCE(SUM(pnl_quote),0) v FROM positions WHERE mode='paper' "
                              "AND status='closed'").fetchone()
        return self.s.paper_starting_balance_eth + float(r["v"]) - self.exposure_eth("paper")

    # --- gate ---------------------------------------------------------------

    def check_entry(self, *, token: str, quote_symbol: str, mode: str, desired_eth: float | None = None) -> RiskVerdict:
        v = RiskVerdict(ok=True)
        killed, why = self.kill_active()
        if killed:
            v.reasons.append(why)
        if mode == TradingMode.LIVE.value:
            if self.s.trading_mode != TradingMode.LIVE:
                v.reasons.append("trading mode is paper")
            if not self.s.live_execution_reviewed:
                v.reasons.append("live execution adapter not marked reviewed")
            if not self.s.trading_wallet_private_key:
                v.reasons.append("no trading wallet key configured")
        if quote_symbol != "ETH":
            v.reasons.append(f"quote asset {quote_symbol} not supported by ETH risk limits")
        open_pos = self.open_positions(mode)
        if any(p["token"].lower() == token.lower() for p in open_pos):
            v.reasons.append("already holding this token")
        if len(open_pos) >= self.s.max_open_positions:
            v.reasons.append(f"max open positions ({self.s.max_open_positions}) reached")
        since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
        opened = self.conn.execute("SELECT COUNT(*) c FROM positions WHERE mode=? AND opened_at>=?",
                                   (mode, since)).fetchone()["c"]
        if opened >= self.s.max_entries_per_day:
            v.reasons.append(f"max {self.s.max_entries_per_day} new trades per 24h reached")
        if self.realized_today_eth(mode) <= -self.s.max_daily_loss_eth:
            v.reasons.append(f"daily loss limit {self.s.max_daily_loss_eth} ETH hit")
        headroom = self.s.max_total_exposure_eth - self.exposure_eth(mode)
        size = min(self.s.max_position_eth if desired_eth is None else desired_eth, self.s.max_position_eth, headroom)
        if mode == TradingMode.PAPER.value:
            size = min(size, self.paper_cash_eth())
        if size <= 0.0005:
            v.reasons.append("no exposure headroom left")
        v.size_eth = max(0.0, round(size, 6))
        v.ok = not v.reasons
        return v
