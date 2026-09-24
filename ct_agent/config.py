"""Runtime settings, loaded from environment variables or a .env file.

Every secret is optional so the system can boot in paper mode with nothing
configured. Features that need a missing credential disable themselves and
say so in /health rather than crashing.
"""
from __future__ import annotations

from enum import Enum
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class TradingMode(str, Enum):
    PAPER = "paper"
    LIVE = "live"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="CT_", extra="ignore")

    # --- identity -------------------------------------------------------
    agent_name: str = "MOOSE"
    agent_ticker: str = ""  # set once the owner picks one
    agent_token_address: str = ""  # the project's own token, once launched
    treasury_address: str = ""     # public treasury/creator-fee wallet, for disclosure only

    # --- storage --------------------------------------------------------
    database_path: str = "data/ct_agent.db"

    # --- Robinhood Chain ------------------------------------------------
    chain_id: int = 4663
    rpc_url: str = "https://rpc.mainnet.chain.robinhood.com"  # public, rate limited
    # Log scans (eth_getLogs). Alchemy free tier allows only 10 blocks per query,
    # so by default these go to the public node, which allows 10,000 logs per query.
    logs_rpc_url: str = "https://rpc.mainnet.chain.robinhood.com"
    explorer_url: str = "https://robinhoodchain.blockscout.com"
    pons_v2_factory: str = "0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e"
    # First block to index from. 0 means "start near head" on first run.
    pons_start_block: int = 0
    log_chunk_blocks: int = 5_000          # curve trades: ~0.7 logs/block, provider cap 10k logs/query
    launch_log_chunk_blocks: int = 200_000  # factory events are sparse
    launch_backfill_hours: float = 168      # how far back to index launches on first run
    trade_retention_hours: float = 2        # rolling window of curve trades kept locally
    rpc_timeout_s: float = 20.0

    # --- market data ----------------------------------------------------
    geckoterminal_network: str = "robinhood"
    dexscreener_chain: str = "robinhood"

    # --- screening thresholds (prototype filters, not a strategy) -------
    almost_bonded_ratio: float = 0.85
    min_migrated_volume_h1_usd: float = 5_000
    min_migrated_liquidity_usd: float = 15_000
    snipe_window_fallback_s: int = 15  # used only if the factory read fails; live value is read onchain
    # Robinhood Chain blocks are ~0.1s, so windows are in seconds, not blocks.
    activity_windows_s: list[int] = Field(default_factory=lambda: [300, 900, 3_600])
    trade_lookback_s: int = 3_600

    # --- Muse Spark -----------------------------------------------------
    muse_api_key: str = ""
    muse_base_url: str = "https://api.meta.ai/v1"
    muse_model: str = "muse-spark-1.3"
    # tried in order when the main model answers model_not_found
    muse_fallback_models: list[str] = Field(default_factory=lambda: ["muse-spark-1.2"])
    muse_timeout_s: float = 60.0

    # --- X --------------------------------------------------------------
    x_bearer_token: str = ""        # app-only token for reads
    x_user_access_token: str = ""   # OAuth2 user token for posting
    # OAuth 1.0a keys from the X developer portal (Consumer Key/Secret and the
    # @MooseOnMuse Access Token/Secret). These don't expire, so posting never needs a re-login.
    x_api_key: str = ""
    x_api_secret: str = ""
    x_access_token: str = ""
    x_access_secret: str = ""
    # OAuth 2.0 app credentials, only needed if the portal won't issue an access token
    x_client_id: str = ""
    x_client_secret: str = ""
    # Written by `ctagent x-login`; the server seeds its token store from it
    x_refresh_token: str = ""
    x_posting_enabled: bool = False
    x_daily_post_cap: int = 60  # safety net only; trade posts are sent first
    x_update_step_pct: float = 30.0  # post an update under the entry at every +30% (30, 60, 90, ...)
    x_update_down_pct: list[float] = [-15.0]  # and once when it's down this much
    x_max_reads_per_search: int = 20
    x_monthly_read_budget: int = 6_000

    # --- Twitch ---------------------------------------------------------
    twitch_client_id: str = ""
    twitch_client_secret: str = ""
    twitch_eventsub_secret: str = ""
    twitch_channels: list[str] = Field(default_factory=list)

    # --- news -----------------------------------------------------------
    news_feeds: list[str] = Field(default_factory=list)

    # --- trading & risk -------------------------------------------------
    trading_mode: TradingMode = TradingMode.PAPER
    paper_starting_balance_eth: float = 1.0
    max_position_eth: float = 0.05
    max_open_positions: int = 3
    max_total_exposure_eth: float = 0.15
    max_daily_loss_eth: float = 0.05
    max_slippage_bps: int = 300
    max_trade_tax_bps: int = 1_000
    kill_switch_file: str = "data/KILL"
    stop_loss_pct: float = 30.0
    # Take-profit ladder: (price multiple of entry, share of the ORIGINAL position to sell).
    # After the first rung the stop moves to entry. Whatever the rungs don't sell rides until the stop
    # or max_hold_hours, when it is sold.
    take_profit_ladder: list[tuple[float, float]] = [(1.6, 0.50), (2.0, 0.25)]
    max_hold_hours: float = 24.0
    # 0 = a buy can rest on onchain activity alone; 1+ = that many verified/first-party/reported sources required
    min_independent_sources_for_buy: int = 0
    max_deployer_buy_share: float = 0.30
    max_top_buyer_share: float = 0.40
    decision_cooldown_min: int = 30  # don't re-ask Muse about the same token too often
    candidates_per_cycle: int = 5
    site_origins: list[str] = ["https://www.mooseonmuse.com", "https://mooseonmuse.com",
                               "https://moose-theta.vercel.app", "http://127.0.0.1:5173", "http://localhost:5173"]
    # Public "Ask Muse" chat on the website. Every request is a paid Muse call, so it is capped per
    # visitor and overall; the global cap is the hard ceiling on spend.
    chat_enabled: bool = True
    chat_per_ip_hour: int = 10
    chat_per_ip_day: int = 30
    chat_global_day: int = 500
    chat_max_chars: int = 500
    chat_x_lookups_day: int = 50  # X profile look-ups the chat may make per day (MOOSE's own research is separate)
    max_entries_per_day: int = 10  # hard cap on new trades in any rolling 24h
    research_wait_min: int = 10  # wait this long for a token's X research before deciding without it

    # Live trading wallet. Only the capped trading wallet key ever lives here.
    # The owner and treasury keys must never be given to this process.
    trading_wallet_private_key: str = ""
    live_execution_reviewed: bool = False  # flipped only after a human review

    # --- API ------------------------------------------------------------
    api_admin_token: str = ""  # required for mutating endpoints


@lru_cache
def get_settings() -> Settings:
    return Settings()
