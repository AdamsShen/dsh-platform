"""P5 扇出：主体目录、发布期校验、按主体建 run、重叠与上限（W10~W15）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ao import triggers as trigger_store
from ao import users as users_store
from ao.db import fetch_all, tx
from ao.manifest import parse
from ao.runs import create_run, ensure_workflow
from ao.settings import settings
from ao.util import new_id
from ao.web import create_app


@pytest.fixture
def tenant() -> str:
    """每个用例一个独立租户：users 表不在 conftest 的 TRUNCATE 列表里，靠租户隔离。"""
    tenant_id = new_id("tnt")
    with tx() as conn:
        conn.execute(
            "INSERT INTO tenants (id, name) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (tenant_id, tenant_id),
        )
    return tenant_id


def _seed_users(tenant_id: str, count: int) -> list[dict[str, Any]]:
    return [
        users_store.upsert(
            tenant_id=tenant_id,
            external_ref=f"user-{index}",
            display_name=f"用户{index}",
        )
        for index in range(count)
    ]


def _project(
    tenant_id: str, *, fanout: dict[str, Any], overlap: str = "skip"
) -> tuple[str, dict[str, Any]]:
    workflow_id = new_id("wf_fan")
    ensure_workflow(workflow_id, tenant_id=tenant_id, name="扇出测试")
    rows = trigger_store.project(
        workflow_id=workflow_id,
        version=1,
        tenant_id=tenant_id,
        owner_user_id="u_owner",
        package_ref="pkg://test/fanout",
        triggers=[
            {
                "type": "cron",
                "cron": "0 9 * * *",
                "timezone": "UTC",
                "overlap_policy": overlap,
                "fanout": fanout,
            }
        ],
    )
    assert len(rows) == 1
    return workflow_id, rows[0]


def _fire(
    trigger_id: str, moment: datetime | None = None
) -> list[dict[str, Any]]:
    """把计划指针拨到 `moment`（默认一分钟前）再扫一轮。

    需要验证"同一时刻重复投递"时必须显式传同一个 `moment`——否则每次拨的都是
    新槽位，幂等键自然不同，测不到想测的东西。
    """
    slot = moment or datetime.now(UTC) - timedelta(minutes=1)
    with tx() as conn:
        conn.execute(
            "UPDATE triggers SET next_fire_at = %s WHERE id = %s", (slot, trigger_id)
        )
    return trigger_store.fire_due(now=datetime.now(UTC))


def _runs(workflow_id: str) -> list[dict[str, Any]]:
    return fetch_all(
        "SELECT * FROM runs WHERE workflow_id = %s ORDER BY created_at", (workflow_id,)
    )


# ---------------------------------------------------------------- 发布期校验


def test_manifest_rejects_unknown_fanout_mode() -> None:
    with pytest.raises(ValueError, match="fanout.mode"):
        parse(
            {
                "id": "wf_fan_bad",
                "nodes": [{"id": "a"}],
                "triggers": [
                    {"type": "cron", "cron": "0 9 * * *", "fanout": {"mode": "mcp_query"}}
                ],
            }
        )


def test_manifest_rejects_static_list_without_users() -> None:
    with pytest.raises(ValueError, match="user_ids"):
        parse(
            {
                "id": "wf_fan_bad2",
                "nodes": [{"id": "a"}],
                "triggers": [
                    {"type": "cron", "cron": "0 9 * * *", "fanout": {"mode": "static_list"}}
                ],
            }
        )


def test_manifest_rejects_zero_limit_and_manual_fanout() -> None:
    with pytest.raises(ValueError, match="fanout.limit"):
        parse(
            {
                "id": "wf_fan_bad3",
                "nodes": [{"id": "a"}],
                "triggers": [
                    {
                        "type": "cron",
                        "cron": "0 9 * * *",
                        "fanout": {"mode": "tenant_active", "limit": 0},
                    }
                ],
            }
        )
    with pytest.raises(ValueError, match="manual"):
        parse(
            {
                "id": "wf_fan_bad4",
                "nodes": [{"id": "a"}],
                "triggers": [{"type": "manual", "fanout": {"mode": "tenant_active"}}],
            }
        )


# ---------------------------------------------------------------- 投影与展开


def test_project_carries_fanout_columns(tenant: str) -> None:
    _, trigger = _project(
        tenant,
        fanout={"mode": "tenant_active", "limit": 50, "budget_per_run": 0.25},
    )
    assert trigger["fanout_mode"] == "tenant_active"
    assert trigger["fanout_limit"] == 50
    assert float(trigger["budget_per_run"]) == pytest.approx(0.25)
    assert trigger["fanout_spec"] == {}


def test_tenant_active_creates_one_run_per_user(tenant: str) -> None:
    seeded = _seed_users(tenant, 3)
    workflow_id, trigger = _project(tenant, fanout={"mode": "tenant_active"})

    result = _fire(trigger["id"])
    assert [item["status"] for item in result] == ["consumed"]
    assert result[0]["created"] == 3
    assert result[0]["failed"] == 0

    runs = _runs(workflow_id)
    assert len(runs) == 3
    subject_uids = sorted(row["subject"]["user_id"] for row in runs)
    assert subject_uids == ["user-0", "user-1", "user-2"]
    # 平台 id 也写进 subject，便于追溯；trigger_user_id 是主体本人而不是 owner
    assert {row["subject"]["platform_user_id"] for row in runs} == {row["id"] for row in seeded}
    assert {row["trigger_user_id"] for row in runs} == {row["id"] for row in seeded}

    # 一批只写一条审计，汇总里能看到三个数字
    events = fetch_all(
        "SELECT status, payload FROM trigger_events WHERE trigger_id = %s", (trigger["id"],)
    )
    assert len(events) == 1
    payload = events[0]["payload"]
    assert (payload["total"], payload["created"], payload["skipped"], payload["failed"]) == (
        3,
        3,
        0,
        0,
    )


def test_same_slot_does_not_duplicate_runs(tenant: str) -> None:
    _seed_users(tenant, 2)
    workflow_id, trigger = _project(tenant, fanout={"mode": "tenant_active"})
    slot = datetime.now(UTC) - timedelta(minutes=1)
    _fire(trigger["id"], slot)
    assert len(_runs(workflow_id)) == 2

    # 上一轮还是 queued：按主体重叠跳过，不新增
    second = _fire(trigger["id"], slot)
    assert second[0]["created"] == 0
    assert second[0]["skipped"] == 2
    assert len(_runs(workflow_id)) == 2

    # run 结束后同一时刻重扫：幂等键（window 带主体）命中，仍然只有 2 个
    with tx() as conn:
        conn.execute("UPDATE runs SET status = 'succeeded' WHERE workflow_id = %s", (workflow_id,))
    third = _fire(trigger["id"], slot)
    assert third[0]["created"] == 0
    assert third[0]["skipped"] == 2
    assert len(_runs(workflow_id)) == 2


def test_blocked_user_does_not_block_others(tenant: str) -> None:
    _seed_users(tenant, 3)
    workflow_id, trigger = _project(tenant, fanout={"mode": "tenant_active"})
    # user-0 已有一个在跑的 run，其他人不受影响
    create_run(
        tenant_id=tenant,
        workflow_id=workflow_id,
        version=1,
        package_ref="pkg://test/fanout",
        subject={"user_id": "user-0"},
        window="manual#user-0",
    )

    result = _fire(trigger["id"])
    assert result[0]["created"] == 2
    assert result[0]["skipped"] == 1
    runs = _runs(workflow_id)
    assert len(runs) == 3
    assert sum(1 for row in runs if row["subject"].get("scheduled_for")) == 2


def test_static_list_expands_named_users(tenant: str) -> None:
    _seed_users(tenant, 3)
    workflow_id, trigger = _project(
        tenant,
        fanout={"mode": "static_list", "user_ids": ["user-2", "user-0", "ghost"]},
    )
    result = _fire(trigger["id"])
    # 解析不到的 ghost 被跳过，不会让整批失败
    assert result[0]["created"] == 2
    assert sorted(row["subject"]["user_id"] for row in _runs(workflow_id)) == [
        "user-0",
        "user-2",
    ]


# ---------------------------------------------------------------- 上限与预算


def test_fanout_limit_rejects_whole_batch(tenant: str) -> None:
    _seed_users(tenant, 3)
    workflow_id, trigger = _project(
        tenant, fanout={"mode": "tenant_active", "limit": 2}
    )
    result = _fire(trigger["id"])
    assert result[0]["status"] == "failed"
    assert "fanout_limit_exceeded" in result[0]["detail"]
    assert _runs(workflow_id) == []

    events = fetch_all(
        "SELECT status, payload FROM trigger_events WHERE trigger_id = %s", (trigger["id"],)
    )
    assert events[0]["status"] == "failed"
    assert events[0]["payload"]["total"] == 3


def test_budget_cap_rejects_batch(tenant: str, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_users(tenant, 3)
    monkeypatch.setattr(settings, "fanout_budget_cap", 0.02)
    workflow_id, trigger = _project(
        tenant, fanout={"mode": "tenant_active", "budget_per_run": 0.01}
    )
    result = _fire(trigger["id"])
    assert result[0]["status"] == "failed"
    assert "fanout_budget_cap_exceeded" in result[0]["detail"]
    assert _runs(workflow_id) == []


def test_budget_per_run_reaches_run_budget(tenant: str) -> None:
    _seed_users(tenant, 2)
    workflow_id, trigger = _project(
        tenant, fanout={"mode": "tenant_active", "budget_per_run": 0.5}
    )
    _fire(trigger["id"])
    budgets = {float(row["budget_usd"]) for row in _runs(workflow_id)}
    assert budgets == {0.5}


def test_users_api_lists_only_active(tenant: str) -> None:
    _seed_users(tenant, 2)
    users_store.upsert(tenant_id=tenant, external_ref="rejected-one", status="rejected")
    client = TestClient(create_app())

    resp = client.get(f"/api/users?tenant_id={tenant}")
    assert resp.status_code == 200
    assert [item["external_ref"] for item in resp.json()["users"]] == ["user-0", "user-1"]

    created = client.post(
        "/api/users",
        json={"tenant_id": tenant, "external_ref": "user-9", "display_name": "九号"},
    )
    assert created.status_code == 200
    assert created.json()["user"]["external_ref"] == "user-9"

    bad = client.post("/api/users", json={"tenant_id": tenant, "external_ref": ""})
    assert bad.status_code == 400


def test_buffer_one_degrades_to_skip(tenant: str) -> None:
    _seed_users(tenant, 2)
    workflow_id, trigger = _project(
        tenant, fanout={"mode": "tenant_active"}, overlap="buffer_one"
    )
    create_run(
        tenant_id=tenant,
        workflow_id=workflow_id,
        version=1,
        package_ref="pkg://test/fanout",
        subject={"user_id": "user-0"},
        window="manual#user-0",
    )
    result = _fire(trigger["id"])
    # user-0 被跳过（按 skip 语义），user-1 正常建；不写 pending_fire_at
    assert result[0]["created"] == 1
    assert result[0]["skipped"] == 1
    assert result[0]["policy"] == "skip"
    rows = fetch_all("SELECT pending_fire_at FROM triggers WHERE id = %s", (trigger["id"],))
    assert rows[0]["pending_fire_at"] is None
