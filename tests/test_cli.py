"""Every module must import, and the CLI must start: the server runs
`ctagent run-all`, so a syntax error anywhere here stops all of MOOSE."""
import importlib
import pkgutil

import ct_agent
from ct_agent import cli


def test_every_module_imports():
    for m in pkgutil.walk_packages(ct_agent.__path__, "ct_agent."):
        importlib.import_module(m.name)


def test_report_command_prints(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CT_DATABASE_PATH", str(tmp_path / "r.db"))
    from ct_agent.config import get_settings
    get_settings.cache_clear() if hasattr(get_settings, "cache_clear") else None
    assert cli.main(["report"]) == 0
    assert "calls tracked" in capsys.readouterr().out
