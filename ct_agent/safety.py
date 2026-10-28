"""Start-up safety review of the settings.

Runs before any worker or the API starts. Errors are combinations that could lose money or hand someone
control of the bot, and stop the process; warnings are logged loudly and the process carries on.
Never prints a secret's value, only whether it is set.
"""
from __future__ import annotations

import logging
import os

from .config import Settings, TradingMode

log = logging.getLogger(__name__)

MIN_ADMIN_TOKEN_LEN = 32
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
        if not s.live_execution_reviewed:
            warnings.append("CT_TRADING_MODE=live but CT_LIVE_EXECUTION_REVIEWED is false: every live trade is blocked")
        if s.max_position_eth > s.max_total_exposure_eth:
            warnings.append("CT_MAX_POSITION_ETH is above CT_MAX_TOTAL_EXPOSURE_ETH")
        if s.live_venue == "gmgn" and not (s.gmgn_api_key and s.gmgn_private_key):
            warnings.append("CT_LIVE_VENUE=gmgn without CT_GMGN_API_KEY and CT_GMGN_PRIVATE_KEY")
    else:
        if s.trading_wallet_private_key:
            warnings.append("CT_TRADING_WALLET_PRIVATE_KEY is set while in paper mode: it isn't needed, and a key "
                            "that isn't needed is one more key to leak. Remove it until you go live")
        if s.live_execution_reviewed:
            warnings.append("CT_LIVE_EXECUTION_REVIEWED is true while in paper mode")

    if s.trading_wallet_private_key and s.launcher_wallet_private_key and \
            _norm(s.trading_wallet_private_key) == _norm(s.launcher_wallet_private_key):
        errors.append("the trading wallet and the launchpad wallet use the same key: give each its own wallet")
    if s.launchpad_enabled:
        if not s.launcher_wallet_private_key:
            warnings.append("CT_LAUNCHPAD_ENABLED without CT_LAUNCHER_WALLET_PRIVATE_KEY: launches can't be paid for")
        if not s.x_handle:
            errors.append("CT_LAUNCHPAD_ENABLED needs CT_X_HANDLE (the bot's X handle, credited in every launch)")
        if not token:
            warnings.append("CT_LAUNCHPAD_ENABLED with no CT_API_ADMIN_TOKEN")
    if s.chat_enabled and s.grok_api_key and s.chat_global_day <= 0:
        errors.append("CT_CHAT_ENABLED with CT_CHAT_GLOBAL_DAY <= 0: the public chat would have no spending cap")
    if s.x_posting_enabled and not token:
        warnings.append("X posting is on with no CT_API_ADMIN_TOKEN: stuck posts can't be resolved over the API")
    return errors, warnings


def _norm(key: str) -> str:
    return key.strip().lower().removeprefix("0x")


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
