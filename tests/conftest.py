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
        # triggers 之前没清，导致 e2e 用例之间互相污染（上一轮的 trigger 到期会混进
        # fire_due 的结果里）。这里一并截断，测试之间不共享调度状态。
        conn.execute(
            "TRUNCATE runs, run_nodes, run_steps, run_vars, run_events, run_effects, llm_usage,"
            " gateway_keys, planner_messages, planner_sessions, trigger_events, triggers CASCADE"
        )
    yield


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """测试里不要真睡 2 秒。"""
    from ao import settings as settings_module

    monkeypatch.setattr(settings_module.settings, "var_backoff_seconds", 0.0)
