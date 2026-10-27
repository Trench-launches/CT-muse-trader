"""Every module must import, and the CLI must start: the server runs
`ctagent run-all`, so a syntax error anywhere here stops all of HORIZON."""
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


def test_every_worker_run_all_starts_is_a_valid_run_command():
    """run-all starts each worker as `ctagent run <name>`; a name the CLI rejects crash-loops that worker."""
    import subprocess
    import sys
    from ct_agent.workers.runner import WORKERS
    out = subprocess.run([sys.executable, "-m", "ct_agent.cli", "run", "--help"], capture_output=True, text=True)
    assert out.returncode == 0
    for name in WORKERS:
        assert name in out.stdout, name
