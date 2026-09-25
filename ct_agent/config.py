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
    # @MooseByMuse Access Token/Secret). These don't expire, so posting never needs a re-login.
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
    # Posts sent before this time (ISO) belong to a previous X account: follow-ups post on their own
    # instead of replying under them. Set when the posting account changes.
    x_threads_since: str = ""
    # Auto-replies to people who mention or reply to the account (see social/replies.py)
    x_auto_reply_enabled: bool = False
    x_replies_per_day: int = 30
    x_replies_per_user_day: int = 2
    x_mentions_poll_min: float = 3.0
    # Original crypto musings (social/musings.py), on top of trade posts and replies
    x_musings_enabled: bool = False
    x_musing_every_min: float = 30.0
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
    max_position_eth: float = 0.05     # hard cap on one trade, whatever the sizing rule says
    # Size each trade as this % of the account (wallet ETH + open trades at cost; on paper: the paper
    # balance). 0 = every trade is max_position_eth.
    position_size_pct: float = 0.0
    max_open_positions: int = 3
    max_total_exposure_eth: float = 0.15
    max_daily_loss_eth: float = 0.05
    max_slippage_bps: int = 300
    max_trade_tax_bps: int = 1_000
    kill_switch_file: str = "data/KILL"
    stop_loss_pct: float = 70.0
    # Take-profit ladder: (price multiple of entry, share of the ORIGINAL position to sell).
    # After the first rung the stop moves to entry. Whatever the rungs don't sell rides until the stop
    # or max_hold_hours, when it is sold.
    take_profit_ladder: list[tuple[float, float]] = [(1.6, 0.50), (2.0, 0.25)]
    max_hold_hours: float = 24.0
    # 0 = a buy can rest on onchain activity alone; 1+ = that many verified/first-party/reported sources required
    min_independent_sources_for_buy: int = 0
    max_deployer_buy_share: float = 0.30
    # Thin-chart guard: above this market cap, a coin must have paid at least this much in trading fees
    # over its life, or the chart was likely pumped by a handful of (bundled or botted) trades.
    fee_check_mc_usd: float = 20_000
    min_fees_paid_eth: float = 0.02
    # Holder guard: skip coins whose deployer still holds too much of the supply, or whose 10 biggest wallets
    # (not counting the curve, the pool or burn addresses) hold too much: they can dump on everyone else.
    max_deployer_hold_share: float = 0.10
    max_top10_hold_share: float = 0.35
    max_top_buyer_share: float = 0.40
    # Where coins come from: "scanner" (the smart-money scanner's calls in Supabase) or "pons" (MOOSE's own
    # Pons auto-scan of near-bonded and freshly graduated coins).
    candidate_source: str = "scanner"
    supabase_url: str = ""
    supabase_key: str = ""
    # Fibbot: a second source of calls, in its own Supabase project
    fibbot_supabase_url: str = ""
    fibbot_supabase_key: str = ""
    scanner_trigger_pct: float = 30.0   # a call is only a candidate once the coin is up this much from the call
    scanner_window_hours: float = 6.0   # ...and only within this long of the call
    decision_cooldown_min: int = 30  # don't re-ask Muse about the same token too often
    candidates_per_cycle: int = 5
    # Paper-trading history from MOOSE v1 (before this journal), merged into the site's scorecard and labelled
    # as such. JSON, e.g. {"trades": 100, "wins": 54, "losses": 46, "best_eth": 0.3}; optional: net_eth,
    # worst_eth, best_symbol, worst_symbol, avg_hold_h. Empty = no v1 history shown.
    score_history_v1: dict = {}
    # The site's scorecard counts trades opened from this time on (ISO, UTC). A reset keeps the history.
    score_since: str = ""
    site_origins: list[str] = ["https://www.moosebymuse.com", "https://moosebymuse.com",
                               "https://www.mooseonmuse.com", "https://mooseonmuse.com",
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
    # Uniswap on Robinhood Chain (Uniswap/contracts deployments/json/4663.json; the router is the one real
    # swaps on this chain go through). Graduated coins are bought and sold in their Pons ETH pool here.
    uniswap_universal_router: str = "0x204FAca1764B154221e35c0d20aBb3c525710498"
    uniswap_v4_quoter: str = "0x8dc178efb8111bb0973dd9d722ebeff267c98f94"
    permit2: str = "0x000000000022D473030F116dDEE9F6B43aC78BA3"
    live_execution_reviewed: bool = False  # flipped only after a human review

    # --- API ------------------------------------------------------------
    api_admin_token: str = ""  # required for mutating endpoints


@lru_cache
def get_settings() -> Settings:
    return Settings()
