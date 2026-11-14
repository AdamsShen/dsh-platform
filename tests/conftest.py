from __future__ import annotations

import pytest

from ao.db import close_pool, tx
from ao.migrate import migrate


@pytest.fixture(scope="session", autouse=True)
def database() -> None:
    migrate()
    yield
    close_pool()


@pytest.fixture(autouse=True)
def clean_runs() -> None:
    with tx() as conn:
        conn.execute(
            "TRUNCATE runs, run_nodes, run_steps, run_vars, run_events, llm_usage,"
            " planner_messages, planner_sessions CASCADE"
        )
    yield


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """测试里不要真睡 2 秒。"""
    from ao import settings as settings_module

    monkeypatch.setattr(settings_module.settings, "var_backoff_seconds", 0.0)
