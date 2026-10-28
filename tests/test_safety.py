"""Start-up safety review: safe defaults, and dangerous combinations refused or warned about."""
import pytest

from ct_agent import cli
from ct_agent.config import Settings, TradingMode
from ct_agent.safety import CHECKED_ENV, UnsafeSettings, enforce, review

KEY = "0x" + "11" * 32
TOKEN = "t" * 40


def _s(settings, **kw) -> Settings:
    return settings.model_copy(update=kw)


def test_defaults_are_safe(tmp_path):
    s = Settings(_env_file=None)
    assert s.trading_mode == TradingMode.PAPER
    assert not s.live_execution_reviewed and not s.trading_wallet_private_key
    assert not s.x_posting_enabled and not s.x_auto_reply_enabled and not s.x_musings_enabled
    assert not s.launchpad_enabled and not s.chat_enabled
    assert not s.api_admin_token
    assert s.x_handle == "" and s.skip_tokens == []
    assert all("localhost" in o or "127.0.0.1" in o for o in s.site_origins)
    assert review(s) == ([], [])


def test_live_without_admin_token_is_refused(settings):
    errors, _ = review(_s(settings, trading_mode=TradingMode.LIVE, trading_wallet_private_key=KEY,
                          live_execution_reviewed=True))
    assert any("kill switch" in e for e in errors)


def test_short_admin_token_refused_live_warned_on_paper(settings):
    live = _s(settings, trading_mode=TradingMode.LIVE, api_admin_token="short")
    assert any("only 5 characters" in e for e in review(live)[0])
    errors, warnings = review(_s(settings, api_admin_token="short"))
    assert not errors and any("only 5 characters" in w for w in warnings)


def test_live_with_long_token_starts(settings):
    errors, warnings = review(_s(settings, trading_mode=TradingMode.LIVE, api_admin_token=TOKEN,
                                 trading_wallet_private_key=KEY, live_execution_reviewed=True))
    assert errors == [] and warnings == []


def test_live_missing_pieces_are_warned(settings):
    _, warnings = review(_s(settings, trading_mode=TradingMode.LIVE, api_admin_token=TOKEN))
    assert any("CT_TRADING_WALLET_PRIVATE_KEY" in w for w in warnings)
    assert any("CT_LIVE_EXECUTION_REVIEWED" in w for w in warnings)


def test_key_in_paper_mode_is_warned(settings):
    errors, warnings = review(_s(settings, trading_wallet_private_key=KEY))
    assert not errors and any("paper mode" in w for w in warnings)


def test_shared_trading_and_launcher_key_is_refused(settings):
    errors, _ = review(_s(settings, trading_wallet_private_key=KEY, launcher_wallet_private_key=KEY.upper()[2:]))
    assert any("same key" in e for e in errors)


def test_launchpad_needs_handle(settings):
    errors, _ = review(_s(settings, launchpad_enabled=True, launcher_wallet_private_key=KEY, api_admin_token=TOKEN))
    assert any("CT_X_HANDLE" in e for e in errors)
    assert review(_s(settings, launchpad_enabled=True, launcher_wallet_private_key=KEY, api_admin_token=TOKEN,
                     x_handle="bot")) == ([], [])


def test_a_comment_read_as_a_value_is_refused(settings):
    errors, _ = review(_s(settings, x_bearer_token="# app-only token for reads"))
    assert any("CT_X_BEARER_TOKEN looks like a comment" in e for e in errors)


def test_env_example_parses_to_safe_settings():
    """.env.example, copied as-is, must give the safe defaults and no comment-shaped values."""
    from pathlib import Path
    example = Path(__file__).resolve().parent.parent / ".env.example"
    s = Settings(_env_file=str(example))
    assert review(s) == ([], [])
    assert s.trading_mode == TradingMode.PAPER and not s.x_posting_enabled and not s.launchpad_enabled
    assert not s.chat_enabled and not s.trading_wallet_private_key and not s.api_admin_token
    text = example.read_text(encoding="utf-8")
    assert [k for k in Settings.model_fields if f"CT_{k.upper()}=" not in text] == []  # every setting documented


def test_review_never_prints_secret_values(settings):
    secret = "s3cr3t-" + "x" * 10
    errors, warnings = review(_s(settings, trading_mode=TradingMode.LIVE, api_admin_token=secret,
                                 trading_wallet_private_key=KEY, launcher_wallet_private_key=KEY))
    text = " ".join(errors + warnings)
    assert secret not in text and KEY[2:] not in text


def test_enforce_raises_and_is_skipped_in_children(settings, monkeypatch):
    bad = _s(settings, trading_mode=TradingMode.LIVE)
    monkeypatch.delenv(CHECKED_ENV, raising=False)
    with pytest.raises(UnsafeSettings):
        enforce(bad)
    monkeypatch.setenv(CHECKED_ENV, "1")
    enforce(bad)  # run-all already reviewed these settings


def test_cli_refuses_to_start_unsafe(tmp_path, monkeypatch, capsys):
    from ct_agent.config import get_settings
    monkeypatch.delenv(CHECKED_ENV, raising=False)
    monkeypatch.setenv("CT_DATABASE_PATH", str(tmp_path / "s.db"))
    monkeypatch.setenv("CT_TRADING_MODE", "live")
    monkeypatch.setenv("CT_API_ADMIN_TOKEN", "")
    get_settings.cache_clear()
    try:
        assert cli.main(["run", "indexer", "--once"]) == 2
        assert "refusing to start" in capsys.readouterr().err
    finally:
        get_settings.cache_clear()
