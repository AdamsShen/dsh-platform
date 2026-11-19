"""M2 运维面：停止 / 重试 / 审计三条路由（需求 #12）。"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from ao import state as run_state
from ao.db import fetch_all, tx
from ao.runs import create_run, ensure_workflow
from ao.settings import settings
from ao.util import new_id
from ao.web import create_app


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


@pytest.fixture
def run_id() -> str:
    workflow_id = new_id("wf_ops")
    ensure_workflow(workflow_id, tenant_id="default", name="运维面测试")
    created, _ = create_run(
        tenant_id="default",
        workflow_id=workflow_id,
        version=1,
        package_ref="pkg://test/ops",
        subject={"user_id": "user-0"},
        window="ops",
    )
    return created


def _status(run_id: str) -> dict[str, Any]:
    rows = fetch_all("SELECT status, epoch, cancel_requested FROM runs WHERE id = %s", (run_id,))
    return dict(rows[0])


def test_cancel_queued_run_is_immediate(client: TestClient, run_id: str) -> None:
    resp = client.post(f"/api/runs/{run_id}/cancel", headers={"X-User-Id": "ops-1"})
    assert resp.status_code == 200
    assert resp.json()["result"] == "cancelled"
    assert _status(run_id)["status"] == "cancelled"

    actions = fetch_all(
        "SELECT actor, action, result FROM operator_actions WHERE object_id = %s", (run_id,)
    )
    assert actions[0]["actor"] == "ops-1"
    assert actions[0]["action"] == "run.cancel"
    assert actions[0]["result"] == "ok"


def test_cancel_running_run_only_flags(client: TestClient, run_id: str) -> None:
    with tx() as conn:
        conn.execute("UPDATE runs SET status = 'running' WHERE id = %s", (run_id,))
    resp = client.post(f"/api/runs/{run_id}/cancel")
    assert resp.status_code == 200
    assert resp.json()["result"] == "cancelling"
    row = _status(run_id)
    assert row["status"] == "running"
    assert row["cancel_requested"] is True


def test_cancel_missing_run_returns_404(client: TestClient) -> None:
    assert client.post("/api/runs/run_missing/cancel").status_code == 404


def test_retry_failed_run_requeues_with_new_epoch(client: TestClient, run_id: str) -> None:
    with tx() as conn:
        conn.execute(
            "UPDATE runs SET status = 'failed', error = 'boom', ended_at = now() WHERE id = %s",
            (run_id,),
        )
    before = _status(run_id)["epoch"]

    resp = client.post(f"/api/runs/{run_id}/retry", headers={"X-User-Id": "ops-2"})
    assert resp.status_code == 200
    after = _status(run_id)
    assert after["status"] == "queued"
    assert after["epoch"] == before + 1

    events = fetch_all(
        "SELECT type, actor FROM run_events WHERE run_id = %s ORDER BY seq DESC LIMIT 1",
        (run_id,),
    )
    assert events[0]["type"] == "run.requeued"
    assert events[0]["actor"] == "ops-2"


def test_retry_succeeded_run_is_rejected(client: TestClient, run_id: str) -> None:
    with tx() as conn:
        conn.execute("UPDATE runs SET status = 'succeeded' WHERE id = %s", (run_id,))
    resp = client.post(f"/api/runs/{run_id}/retry")
    assert resp.status_code == 409
    actions = fetch_all(
        "SELECT result FROM operator_actions WHERE object_id = %s AND action = 'run.retry'",
        (run_id,),
    )
    assert actions[0]["result"] == "failed"


def test_audit_query_filters_and_returns_events(client: TestClient, run_id: str) -> None:
    client.post(f"/api/runs/{run_id}/cancel", headers={"X-User-Id": "ops-3"})
    resp = client.get(f"/api/audit?object_id={run_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert [row["action"] for row in body["actions"]] == ["run.cancel"]
    assert body["events"], "指定 object_id 时应同时返回 run_events"

    other = client.get("/api/audit?actor=nobody")
    assert other.status_code == 200
    assert other.json()["actions"] == []


def test_denied_attempt_is_recorded(
    client: TestClient, run_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 配了令牌但请求不带：拒绝并且留痕
    monkeypatch.setattr(settings, "ops_token", "secret-token")
    resp = client.post(f"/api/runs/{run_id}/cancel", headers={"X-User-Id": "intruder"})
    assert resp.status_code == 401

    actions = fetch_all(
        "SELECT actor, result, detail FROM operator_actions WHERE object_id = %s", (run_id,)
    )
    assert actions[0]["actor"] == "intruder"
    assert actions[0]["result"] == "denied"
    assert "令牌" in actions[0]["detail"]

    # 带上正确令牌则放行
    ok = client.post(
        f"/api/runs/{run_id}/cancel",
        headers={"X-User-Id": "ops-4", "X-AO-Ops-Token": "secret-token"},
    )
    assert ok.status_code == 200


def test_requeue_only_accepts_failed_or_cancelled(run_id: str) -> None:
    with tx() as conn:
        conn.execute("UPDATE runs SET status = 'paused_budget' WHERE id = %s", (run_id,))
    # paused_budget 走 resume（放行预算），不走 requeue
    assert run_state.requeue(run_id) is False
    assert run_state.resume(run_id) is True
    assert _status(run_id)["status"] == "queued"
