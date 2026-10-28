"""Builds the shared services each process needs."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import httpx

from .brain.grok import GrokBrain, openai_completer
from .chain.solana import SolanaRpc
from .config import Settings, get_settings
from .db import open_db
from .execution.jupiter import Jupiter
from .execution.live import LiveExecutor
from .execution.risk import RiskManager
from .market.dex import MarketData
from .sources.x_api import XClient
from .sources.x_oauth import XOAuth2


@dataclass
class Context:
    settings: Settings
    conn: sqlite3.Connection
    rpc: SolanaRpc
    market: MarketData
    jupiter: Jupiter
    brain: GrokBrain
    x: XClient
    risk: RiskManager
    live: LiveExecutor
    web: httpx.Client | None = None  # reads coin websites for their lore, and the scanner's Supabase


def build(settings: Settings | None = None, *, rpc_transport: httpx.BaseTransport | None = None,
          market_transport: httpx.BaseTransport | None = None, x_transport: httpx.BaseTransport | None = None,
          jupiter_transport: httpx.BaseTransport | None = None, completer=None) -> Context:
    s = settings or get_settings()
    conn = open_db(s.database_path)
    rpc = SolanaRpc(s.solana_rpc_url, backup_url=s.solana_backup_rpc_url or None, timeout=s.rpc_timeout_s,
                    transport=rpc_transport)
    market = MarketData(dexscreener_chain=s.dexscreener_chain, gt_network=s.geckoterminal_network,
                        transport=market_transport)
    jupiter = Jupiter(s.jupiter_api_url, s.jupiter_api_key, transport=jupiter_transport)
    if jupiter_transport is not None:  # a test double: no rate limit to pace for
        jupiter.min_interval_s = 0.0
    if completer is None and s.grok_api_key:
        completer = openai_completer(s.grok_api_key, s.grok_base_url, s.grok_model, s.grok_timeout_s,
                                     fallback_models=s.grok_fallback_models)
    brain = GrokBrain(completer, s.grok_model)
    oauth = XOAuth2(client_id=s.x_client_id, client_secret=s.x_client_secret, conn=conn,
                    seed_refresh_token=s.x_refresh_token, transport=x_transport) if s.x_client_id else None
    x = XClient(bearer_token=s.x_bearer_token, user_token=s.x_user_access_token, oauth=oauth,
                monthly_read_budget=s.x_monthly_read_budget, conn=conn, transport=x_transport)
    risk = RiskManager(s, conn)
    live = LiveExecutor(s, rpc, jupiter, conn, risk)
    web = httpx.Client(timeout=6, transport=market_transport)
    return Context(s, conn, rpc, market, jupiter, brain, x, risk, live, web)
