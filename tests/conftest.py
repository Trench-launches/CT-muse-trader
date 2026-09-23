import pytest

from ct_agent.config import Settings
from ct_agent.db import open_db


@pytest.fixture
def db(tmp_path):
    conn = open_db(str(tmp_path / "test.db"))
    yield conn
    conn.close()


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        database_path=str(tmp_path / "test.db"),
        kill_switch_file=str(tmp_path / "KILL"),
    )
