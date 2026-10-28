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
    agent_name: str = "VIGIL"
    agent_ticker: str = "VIGIL"  # the project's own coin; the launchpad won't launch a copy of it
    agent_token_address: str = ""  # the project's own token, once launched
    # Never traded from scanner calls, like our own token: e.g. an older project coin (JSON list of addresses).
    skip_tokens: list[str] = []
    treasury_address: str = ""     # public treasury/creator-fee wallet, for disclosure only

    # --- storage --------------------------------------------------------
    database_path: str = "data/vigil.db"  # Solana build: a fresh file, nothing shared with the Robinhood-era DB

    # --- Solana -------------------------------------------------------------
    # Use a provider URL with its key (e.g. Helius: https://mainnet.helius-rpc.com/?api-key=...): the public node is
    # rate limited and "not intended for production". The backup answers when the main one is out of retries.
    solana_rpc_url: str = "https://api.mainnet-beta.solana.com"
    solana_backup_rpc_url: str = "https://api.mainnet-beta.solana.com"
    rpc_timeout_s: float = 20.0
    explorer_url: str = "https://solscan.io"

    # --- Jupiter: swaps and prices (free key from developers.jup.ag) -----------
    jupiter_api_url: str = "https://api.jup.ag"
    jupiter_api_key: str = ""

    # --- market data ----------------------------------------------------
    geckoterminal_network: str = "solana"
    dexscreener_chain: str = "solana"

    # --- screening thresholds (prototype filters, not a strategy) -------
    min_volume_h1_usd: float = 5_000
    min_liquidity_usd: float = 15_000
    max_transfer_fee_bps: int = 0  # Token-2022 coins that tax every transfer are skipped (0 = any tax)

    # --- Grok (xAI API, key from console.x.ai; not the X developer keys) ----
    grok_api_key: str = ""
    grok_base_url: str = "https://api.x.ai/v1"
    grok_model: str = "grok-4.7"
    # tried in order when the main model answers model_not_found
    grok_fallback_models: list[str] = Field(default_factory=lambda: ["grok-4.3"])
    grok_timeout_s: float = 90.0  # reasoning models think before they answer

    # --- X --------------------------------------------------------------
    x_bearer_token: str = ""        # app-only token for reads
    x_user_access_token: str = ""   # OAuth2 user token for posting
    # The bot's X handle, without the @: shown in launch credits. Change it when the account is renamed.
    x_handle: str = ""
    # OAuth 1.0a keys from the X developer portal (Consumer Key/Secret and the
    # bot account's Access Token/Secret). These don't expire, so posting never needs a re-login.
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
    # Goal posts (social/goals.py): what VIGIL is aiming for next and what its own data has taught it, in the cadence
    x_goals_enabled: bool = False
    x_goals_every_hours: float = 6.0
    # A timeline post at least this often (social/cadence.py): trades count, and gaps are filled in rotation with a
    # scan report, the open bags, a musing, the scorecard and a goal post. 0 turns it off (musings then run on their
    # own timer).
    x_cadence_min: float = 17.0
    x_daily_post_cap: int = 60  # safety net only; trade posts are sent first
    x_update_step_pct: float = 50.0  # post an update, with Grok's updated thesis, at every +50% (50, 100, 150, ...)
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
    paper_starting_balance_sol: float = 10.0
    max_position_sol: float = 0.5      # hard cap on one trade, whatever the sizing rule says
    # Size each trade as this % of the account (wallet SOL + open trades at cost; on paper: the paper
    # balance). 0 = every trade is max_position_sol.
    position_size_pct: float = 0.0
    max_open_positions: int = 3
    max_total_exposure_sol: float = 1.5
    max_daily_loss_sol: float = 0.5
    max_slippage_bps: int = 300
    kill_switch_file: str = "data/KILL"
    stop_loss_pct: float = 70.0
    # Take-profit ladder: (price multiple of entry, share of the ORIGINAL position to sell).
    # After the first rung the stop moves to entry. Whatever the rungs don't sell rides until the stop
    # or max_hold_hours, when it is sold.
    take_profit_ladder: list[tuple[float, float]] = [(1.5, 0.50), (2.0, 0.25)]
    max_hold_hours: float = 24.0
    # 0 = a buy can rest on onchain activity alone; 1+ = that many verified/first-party/reported sources required
    min_independent_sources_for_buy: int = 0
    max_deployer_buy_share: float = 0.30
    # Holder guard: skip coins whose deployer still holds too much of the supply, or whose 10 biggest wallets
    # (not counting the curve, the pool or burn addresses) hold too much: they can dump on everyone else.
    max_deployer_hold_share: float = 0.10
    max_top10_hold_share: float = 0.35
    max_top_buyer_share: float = 0.40
    # Coins come from the smart-money scanner's Solana calls (Supabase alert_performance, plus Fibbot's).
    supabase_url: str = ""
    supabase_key: str = ""
    # Fibbot: a second source of calls, in its own Supabase project
    fibbot_supabase_url: str = ""
    fibbot_supabase_key: str = ""
    scanner_trigger_pct: float = 30.0   # a call is only a candidate once the coin is up this much from the call
    scanner_window_hours: float = 6.0   # ...and only within this long of the call
    scanner_main_enabled: bool = True   # the main scanner's calls; off = Fibbot's smart-money calls only
    scanner_buy_on_call: bool = False   # buy every tradeable call the moment it lands (no +trigger wait)
    decision_cooldown_min: int = 30  # don't re-ask Grok about the same token too often
    candidates_per_cycle: int = 5
    # Paper-trading history from VIGIL v1 (before this journal), merged into the site's scorecard and labelled
    # as such. JSON, amounts in SOL, e.g. {"trades": 100, "wins": 54, "losses": 46, "best_sol": 0.3}; optional:
    # net_sol, deployed_sol, worst_sol, best_symbol, worst_symbol, avg_hold_h. Empty = no v1 history shown.
    score_history_v1: dict = {}
    # The site's scorecard counts trades opened from this time on (ISO, UTC). A reset keeps the history.
    score_since: str = ""
    # Websites allowed to call the API from a browser (CORS), as a JSON list. Add your site's domain(s).
    site_origins: list[str] = ["http://127.0.0.1:5173", "http://localhost:5173"]
    # Public "Ask VIGIL" chat on the website. Every request is a paid Grok call, so it is capped per
    # visitor and overall; the global cap is the hard ceiling on spend. Off unless you turn it on.
    chat_enabled: bool = False
    chat_per_ip_hour: int = 10
    chat_per_ip_day: int = 30
    chat_global_day: int = 500
    chat_max_chars: int = 500
    chat_x_lookups_day: int = 50  # X profile look-ups the chat may make per day (VIGIL's own research is separate)
    max_entries_per_day: int = 10  # hard cap on new trades in any rolling 24h
    research_wait_min: int = 10  # wait this long for a token's X research before deciding without it

    # Live trading wallet (base58 secret key, as Phantom exports it). Only the capped trading wallet ever lives
    # here: never the owner's or the treasury's.
    trading_wallet_private_key: str = ""
    live_execution_reviewed: bool = False  # flipped only after a human review
    # Where swaps go: "jupiter" (VIGIL signs Jupiter-built swaps itself) or "gmgn" (GMGN's trading API, from the
    # wallet bound to the API key).
    live_venue: str = "jupiter"
    gmgn_api_key: str = ""
    gmgn_private_key: str = ""  # the Ed25519 key GMGN checks request signatures against (not a wallet key)
    gmgn_host: str = "https://openapi.gmgn.ai"
    gmgn_chain: str = "sol"
    gmgn_slippage_pct: float = 0.0  # 0 = GMGN's auto slippage

    # --- Launchpad: coins launched on pump.fun for people who ask on X ---------------
    # Its own wallet (never the trading wallet) pays each launch. The coin's creator fees are split once, at
    # launch, and pump.fun locks the split: launch_creator_share_bps to the requester, the rest to the launcher.
    launchpad_enabled: bool = False
    launcher_wallet_private_key: str = ""
    launch_daily_cap_sol: float = 0.5      # total launch spend per 24h; past it, requests wait for tomorrow
    launch_dev_buy_sol: float = 0.0        # optional first buy by the launcher at creation (0 = none)
    launch_creator_share_bps: int = 9_000  # the requester's share of creator fees (VIGIL keeps the rest)
    # The fee split is irreversible, so it isn't sent until a real one has been checked: set true once
    # `ctagent launch-test` has locked a split and you've seen it on Solscan.
    launch_fee_split_verified: bool = False
    launch_per_user_day: int = 1           # launches per X account per 24h
    launch_vip_handles: list[str] = []     # X handles with a higher daily limit (the team, for tests)
    launch_vip_per_day: int = 10
    launch_min_account_days: int = 30      # the requester's X account must be at least this old
    pumpportal_url: str = "https://pumpportal.fun/api/trade-local"
    pinata_jwt: str = ""                   # uploads each coin's image + metadata to IPFS (pinata.cloud)

    # --- API ------------------------------------------------------------
    api_admin_token: str = ""  # required for mutating endpoints; at least 32 random characters (see safety.py)


@lru_cache
def get_settings() -> Settings:
    return Settings()
