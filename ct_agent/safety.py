"""Start-up safety review of the settings.

Runs before any worker or the API starts. Errors are combinations that could lose money or hand someone
control of the bot, and stop the process; warnings are logged loudly and the process carries on.
Never prints a secret's value, only whether it is set.
"""
from __future__ import annotations

import logging
import os

from .chain.keys import is_pubkey, load_keypair
from .config import Settings, TradingMode

log = logging.getLogger(__name__)

MIN_ADMIN_TOKEN_LEN = 32
PUBLIC_RPC = "api.mainnet-beta.solana.com"
# run-all sets this for the processes it starts, so the review is logged once, by the supervisor
CHECKED_ENV = "CT_AGENT_SAFETY_CHECKED"


class UnsafeSettings(RuntimeError):
    pass


def review(s: Settings) -> tuple[list[str], list[str]]:
    """(errors, warnings) for these settings."""
    errors: list[str] = []
    warnings: list[str] = []
    live = s.trading_mode == TradingMode.LIVE
    token = s.api_admin_token

    # `CT_X=   # note` in a .env file sets CT_X to "# note", which would pass for a configured credential
    for name in Settings.model_fields:
        v = getattr(s, name)
        if isinstance(v, str) and v.lstrip().startswith("#"):
            errors.append(f"CT_{name.upper()} looks like a comment, not a value: in .env, put comments on their own line")

    if token and len(token) < MIN_ADMIN_TOKEN_LEN:
        msg = (f"CT_API_ADMIN_TOKEN is only {len(token)} characters; use at least {MIN_ADMIN_TOKEN_LEN} random "
               "characters (python -c \"import secrets; print(secrets.token_urlsafe(32))\")")
        (errors if live else warnings).append(msg)
    if token and token.strip() != token:
        warnings.append("CT_API_ADMIN_TOKEN has leading or trailing whitespace")

    if live:
        if not token:
            errors.append("live trading with no CT_API_ADMIN_TOKEN: the kill switch can't be reached over the API")
        if not s.trading_wallet_private_key:
            warnings.append("CT_TRADING_MODE=live but no CT_TRADING_WALLET_PRIVATE_KEY: every live trade is blocked")
        elif _pubkey(s.trading_wallet_private_key) is None:
            errors.append("CT_TRADING_WALLET_PRIVATE_KEY is not a valid Solana private key (base58, as Phantom "
                          "exports it, or a solana-keygen JSON byte array)")
        if not s.live_execution_reviewed:
            warnings.append("CT_TRADING_MODE=live but CT_LIVE_EXECUTION_REVIEWED is false: every live trade is blocked")
        if s.max_position_sol > s.max_total_exposure_sol:
            warnings.append("CT_MAX_POSITION_SOL is above CT_MAX_TOTAL_EXPOSURE_SOL")
        if s.live_venue not in ("jupiter", "gmgn"):
            errors.append(f"CT_LIVE_VENUE must be jupiter or gmgn, not {s.live_venue!r}")
        if s.live_venue == "gmgn" and not (s.gmgn_api_key and s.gmgn_private_key):
            warnings.append("CT_LIVE_VENUE=gmgn without CT_GMGN_API_KEY and CT_GMGN_PRIVATE_KEY")
        if s.live_venue == "jupiter" and not s.jupiter_api_key:
            warnings.append("live trading with no CT_JUPITER_API_KEY: Jupiter's keyless lane is slow and may refuse "
                            "swaps (free key at developers.jup.ag)")
        if PUBLIC_RPC in s.solana_rpc_url:
            warnings.append("live trading on the public Solana RPC: it is rate limited; set CT_SOLANA_RPC_URL to a "
                            "provider (e.g. Helius)")
    else:
        if s.trading_wallet_private_key:
            warnings.append("CT_TRADING_WALLET_PRIVATE_KEY is set while in paper mode: it isn't needed, and a key "
                            "that isn't needed is one more key to leak. Remove it until you go live")
        if s.live_execution_reviewed:
            warnings.append("CT_LIVE_EXECUTION_REVIEWED is true while in paper mode")

    if s.trading_wallet_private_key and s.launcher_wallet_private_key and \
            _same_wallet(s.trading_wallet_private_key, s.launcher_wallet_private_key):
        errors.append("the trading wallet and the launchpad wallet use the same key: give each its own wallet")
    for name in ("agent_token_address", "treasury_address"):
        v = getattr(s, name)
        if v and not is_pubkey(v):
            warnings.append(f"CT_{name.upper()} is not a Solana address (base58)")
    bad_skips = [t for t in s.skip_tokens if not is_pubkey(t)]
    if bad_skips:
        warnings.append(f"{len(bad_skips)} CT_SKIP_TOKENS entr{'y is' if len(bad_skips) == 1 else 'ies are'} not a "
                        "Solana address (base58, case-sensitive)")
    if s.launchpad_enabled:
        if not s.launcher_wallet_private_key:
            warnings.append("CT_LAUNCHPAD_ENABLED without CT_LAUNCHER_WALLET_PRIVATE_KEY: launches can't be paid for")
        elif _pubkey(s.launcher_wallet_private_key) is None:
            errors.append("CT_LAUNCHER_WALLET_PRIVATE_KEY is not a valid Solana private key")
        if not s.x_handle:
            errors.append("CT_LAUNCHPAD_ENABLED needs CT_X_HANDLE (the bot's X handle, credited in every launch)")
        if not token:
            warnings.append("CT_LAUNCHPAD_ENABLED with no CT_API_ADMIN_TOKEN")
    if s.chat_enabled and s.grok_api_key and s.chat_global_day <= 0:
        errors.append("CT_CHAT_ENABLED with CT_CHAT_GLOBAL_DAY <= 0: the public chat would have no spending cap")
    if s.x_posting_enabled and not token:
        warnings.append("X posting is on with no CT_API_ADMIN_TOKEN: stuck posts can't be resolved over the API")
    return errors, warnings


def _pubkey(secret: str) -> str | None:
    """The wallet address a private key belongs to, or None if it isn't a Solana key. Never the key itself."""
    try:
        return str(load_keypair(secret).pubkey())
    except Exception:
        return None


def _same_wallet(a: str, b: str) -> bool:
    pa, pb = _pubkey(a), _pubkey(b)
    if pa and pb:
        return pa == pb
    return a.strip() == b.strip()  # base58 is case-sensitive: compared exactly


def enforce(s: Settings) -> None:
    """Log the review and raise UnsafeSettings on any error. Skipped in processes run-all started."""
    if os.environ.get(CHECKED_ENV) == "1":
        return
    errors, warnings = review(s)
    for w in warnings:
        log.warning("SAFETY: %s", w)
    for e in errors:
        log.error("SAFETY: %s", e)
    if errors:
        raise UnsafeSettings("refusing to start: " + "; ".join(errors))
