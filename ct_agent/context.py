"""Builds the shared services each process needs."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import httpx

from .brain.muse import MuseBrain, openai_completer
from .chain.pons import PonsV2
from .chain.rpc import RpcClient
from .config import Settings, get_settings
from .db import open_db
from .execution.live import LiveExecutor
from .execution.risk import RiskManager
from .market.dex import MarketData
from .sources.x_api import XClient


@dataclass
class Context:
    settings: Settings
    conn: sqlite3.Connection
    rpc: RpcClient
    pons: PonsV2
    market: MarketData
    brain: MuseBrain
    x: XClient
    risk: RiskManager
    live: LiveExecutor


def build(settings: Settings | None = None, *, rpc_transport: httpx.BaseTransport | None = None,
          market_transport: httpx.BaseTransport | None = None, x_transport: httpx.BaseTransport | None = None,
          completer=None) -> Context:
    s = settings or get_settings()
    conn = open_db(s.database_path)
    rpc = RpcClient(s.rpc_url, timeout=s.rpc_timeout_s, transport=rpc_transport)
    pons = PonsV2(rpc, s.pons_v2_factory, log_chunk=s.log_chunk_blocks)
    market = MarketData(dexscreener_chain=s.dexscreener_chain, gt_network=s.geckoterminal_network,
                        transport=market_transport)
    if completer is None and s.muse_api_key:
        completer = openai_completer(s.muse_api_key, s.muse_base_url, s.muse_model, s.muse_timeout_s)
    brain = MuseBrain(completer, s.muse_model)
    x = XClient(bearer_token=s.x_bearer_token, user_token=s.x_user_access_token,
                monthly_read_budget=s.x_monthly_read_budget, conn=conn, transport=x_transport)
    risk = RiskManager(s, conn)
    live = LiveExecutor(s, rpc, pons, conn, risk)
    return Context(s, conn, rpc, pons, market, brain, x, risk, live)
