"""C 期：定时触发——cron 计算、发布投影、到点扫描与幂等。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ao import manifest as manifest_mod
from ao import triggers as trigger_store
from ao.cron import next_fire
from ao.db import fetch_all, fetch_one, tx
from ao.manifest import parse
from ao.planner import draft as draft_store
from ao.planner import orchestrator
from ao.planner import session as planner_session
from ao.runs import create_run, ensure_workflow
from ao.settings import settings
from ao.util import new_id
from ao.web import create_app

PLAN = {
    "nodes": [
        {
            "id": "collect",
            "title": "取数",
            "kind": "agent",
            "duty": "取数",
            "outputs": {"items": "str"},
        },
        {
            "id": "finalize",
            "title": "产出",
            "kind": "agent",
            "duty": "产出",
            "outputs": {"output": "str"},
        },
    ],
    "edges": [{"from": "collect", "to": "finalize"}],
}

SCHEDULED_PLAN = {
    **PLAN,
    "schedule": {"cron": "0 9 * * *", "timezone": "Asia/Shanghai"},
}


class ScriptedModel:
    def __init__(self, plan: dict[str, Any] | None = None) -> None:
        self.plan = plan or PLAN

    async def generate(self, *, step: str, prompt: str) -> dict[str, Any]:
        return self.plan


@pytest.fixture(autouse=True)
def _park_stale_triggers() -> None:
    """每个用例开始前把历史遗留的到期计划推到未来。

    `fire_due` 扫的是全表，前一个用例失败留下的到期行会被下一个用例一起处理，
    导致断言串味。
    """
    with tx() as conn:
        conn.execute(
            "UPDATE triggers SET next_fire_at = now() + interval '1 day'"
            " WHERE enabled AND type = 'cron' AND next_fire_at <= now()"
        )
        # 缓冲也要清：前一个用例留下的 pending 会被 fire_due 的 drain 消费，串味到当前用例
        conn.execute(
            "UPDATE triggers SET pending_fire_at = NULL, pending_since = NULL"
            " WHERE enabled AND pending_fire_at IS NOT NULL"
        )


@pytest.fixture
def dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "packages_dir", tmp_path / "packages")
    monkeypatch.setattr(settings, "mock", True)
    return tmp_path


def test_next_fire_uses_workflow_timezone() -> None:
    # 上海 09:00 == UTC 01:00；UTC 时间作为基准，结果必须换回 UTC
    after = datetime(2026, 11, 14, 0, 0, tzinfo=UTC)
    assert next_fire("0 9 * * *", "Asia/Shanghai", after) == datetime(
        2026, 11, 14, 1, 0, tzinfo=UTC
    )
    assert next_fire("0 9 * * *", "Asia/Shanghai", datetime(2026, 11, 14, 2, 0, tzinfo=UTC)) == (
        datetime(2026, 11, 15, 1, 0, tzinfo=UTC)
    )


def test_manifest_rejects_bad_cron() -> None:
    with pytest.raises(ValueError, match="cron"):
        parse(
            {
                "id": "wf_bad_cron",
                "nodes": [{"id": "a"}],
                "triggers": [{"type": "cron", "cron": "每天九点"}],
            }
        )
    # 6 段（带秒）也不接受：调度循环按分钟粒度走
    with pytest.raises(ValueError, match="5 段"):
        parse(
            {
                "id": "wf_bad_cron",
                "nodes": [{"id": "a"}],
                "triggers": [{"type": "cron", "cron": "0 0 9 * * *"}],
            }
        )


def _seed_workflow(workflow_id: str) -> list[dict[str, Any]]:
    ensure_workflow(workflow_id, tenant_id="default", name="触发测试")
    return trigger_store.project(
        workflow_id=workflow_id,
        version=1,
        tenant_id="default",
        owner_user_id="u_001",
        package_ref="pkg://test/trigger",
        triggers=[{"type": "cron", "cron": "0 9 * * *", "timezone": "Asia/Shanghai"}],
    )


def _set_next(trigger_id: str, moment: datetime) -> None:
    with tx() as conn:
        conn.execute(
            "UPDATE triggers SET next_fire_at = %s WHERE id = %s", (moment, trigger_id)
        )


def _runs(workflow_id: str) -> list[dict[str, Any]]:
    return fetch_all(
        "SELECT * FROM runs WHERE workflow_id = %s ORDER BY created_at", (workflow_id,)
    )


def test_project_switches_plan_on_republish() -> None:
    workflow_id = new_id("wf_trg_switch")
    first = _seed_workflow(workflow_id)
    assert len(first) == 1 and first[0]["enabled"] is True

    second = _seed_workflow(workflow_id)
    rows = trigger_store.for_workflow(workflow_id)
    enabled = [row for row in rows if row["enabled"]]
    assert len(enabled) == 1
    assert enabled[0]["id"] == second[0]["id"]
    assert len(rows) == 2  # 旧计划保留为历史，只是不再生效


def test_fire_due_creates_one_run_and_skips_overlap() -> None:
    workflow_id = new_id("wf_trg_fire")
    trigger = _seed_workflow(workflow_id)[0]
    scheduled = datetime.now(UTC) - timedelta(minutes=1)
    _set_next(trigger["id"], scheduled)

    first = trigger_store.fire_due(now=datetime.now(UTC))
    assert [item["status"] for item in first] == ["consumed"]
    runs = _runs(workflow_id)
    assert len(runs) == 1
    assert runs[0]["tenant_id"] == "default"
    assert runs[0]["trigger_user_id"] == "u_001"
    assert runs[0]["subject"]["scheduled_for"] == scheduled.isoformat()

    # 上一轮还是 queued：同一时刻再扫一次只会记 skipped，不会堆 run
    _set_next(trigger["id"], scheduled)
    again = trigger_store.fire_due(now=datetime.now(UTC))
    assert [item["status"] for item in again] == ["skipped"]
    assert len(_runs(workflow_id)) == 1

    # 上一轮结束后重扫同一时刻：create_run 命中幂等键，仍然只有一个 run
    with tx() as conn:
        conn.execute("UPDATE runs SET status = 'succeeded' WHERE workflow_id = %s", (workflow_id,))
    _set_next(trigger["id"], scheduled)
    third = trigger_store.fire_due(now=datetime.now(UTC))
    assert [item["status"] for item in third] == ["consumed"]
    assert third[0]["created"] is False
    assert len(_runs(workflow_id)) == 1

    # 换一个计划时刻：正常产生第二个 run
    later = datetime.now(UTC) - timedelta(seconds=30)
    _set_next(trigger["id"], later)
    fourth = trigger_store.fire_due(now=datetime.now(UTC))
    assert [item["status"] for item in fourth] == ["consumed"]
    assert fourth[0]["created"] is True
    assert len(_runs(workflow_id)) == 2

    # 审计按"计划时刻"一行：同一时刻被重扫时，覆盖为最终结果（这里是 consumed）
    events = fetch_all(
        "SELECT status FROM trigger_events WHERE trigger_id = %s ORDER BY created_at",
        (trigger["id"],),
    )
    assert [row["status"] for row in events] == ["consumed", "consumed"]


async def test_publish_projects_schedule_from_plan(dirs: Path) -> None:
    session = planner_session.create_session(tenant_id="default", user_id="u_001")
    planner_session.append_message(session["id"], "user", "每天早上 9 点生成日报")
    prepared = await orchestrator.prepare(session["id"], model=ScriptedModel(SCHEDULED_PLAN))
    assert prepared.ok, prepared.issues

    draft = draft_store.Draft.load(session["id"])
    assert draft.triggers == [
        {"type": "cron", "cron": "0 9 * * *", "timezone": "Asia/Shanghai"}
    ]
    packaged = manifest_mod.parse(
        __import__("json").loads(
            (draft_store.draft_dir(session["id"]) / "manifest.json").read_text(encoding="utf-8")
        )
    )
    assert packaged.triggers[0].cron == "0 9 * * *"

    published = await orchestrator.publish_draft(session["id"], prepared=prepared)
    assert published.ok, published.issues

    rows = trigger_store.for_workflow(draft.workflow_id)
    assert len(rows) == 1
    assert rows[0]["enabled"] is True
    assert rows[0]["owner_user_id"] == "u_001"
    assert rows[0]["next_fire_at"] > datetime.now(UTC)


def test_update_draft_rejects_bad_schedule(dirs: Path) -> None:
    session = planner_session.create_session(tenant_id="default", user_id="u_001")
    planner_session.append_message(session["id"], "user", "每天生成日报")
    prepared = __import__("asyncio").run(
        orchestrator.prepare(session["id"], model=ScriptedModel())
    )
    assert prepared.ok, prepared.issues

    result = __import__("asyncio").run(
        orchestrator.update_draft(session["id"], [], schedule={"cron": "*/0 * * * *"})
    )
    assert result.ok is False
    assert result.issues[0]["code"] == "E_TRIGGER_CRON"


def test_web_schedule_saved_to_draft(dirs: Path) -> None:
    client = TestClient(create_app())
    session_id = (
        client.get("/planner/new", follow_redirects=False)
        .headers["location"]
        .rsplit("/", 1)[-1]
    )
    client.post(
        f"/planner/{session_id}/messages",
        data={"content": "每周一早上 8 点生成周报"},
        follow_redirects=False,
    )
    client.post(f"/planner/{session_id}/generate", follow_redirects=False)

    resp = client.post(
        f"/planner/{session_id}/schedule",
        data={"cron": "0 8 * * 1", "timezone": "Asia/Shanghai"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    draft = draft_store.Draft.load(session_id)
    assert draft.triggers == [
        {
            "type": "cron",
            "cron": "0 8 * * 1",
            "timezone": "Asia/Shanghai",
            "catchup_window_seconds": 3600,
            "catchup_limit": 1,
            "overlap_policy": "skip",
        }
    ]
    row = fetch_one(
        "SELECT cron FROM triggers WHERE workflow_id = %s AND enabled",
        (draft.workflow_id,),
    )
    # 只保存草稿不会投影：还没发布
    assert row is None
    page = client.get(f"/planner/{session_id}")
    assert 'value="0 8 * * 1"' in page.text


def _seed_cron(
    workflow_id: str,
    *,
    cron: str = "*/10 * * * *",
    window: int = 3600,
    limit: int = 1,
    policy: str = "skip",
) -> dict[str, Any]:
    ensure_workflow(workflow_id, tenant_id="default", name="回填测试")
    rows = trigger_store.project(
        workflow_id=workflow_id,
        version=1,
        tenant_id="default",
        owner_user_id="u_001",
        package_ref="pkg://test/catchup",
        triggers=[
            {
                "type": "cron",
                "cron": cron,
                "timezone": "Asia/Shanghai",
                "catchup_window_seconds": window,
                "catchup_limit": limit,
                "overlap_policy": policy,
            }
        ],
    )
    return rows[0]


def test_catchup_fills_recent_slots_then_stops_on_overlap() -> None:
    workflow_id = new_id("wf_catchup")
    trigger = _seed_cron(workflow_id, limit=3)
    now = datetime.now(UTC)
    # 停机 25 分钟（每 10 分钟一次）：应有 3 个槽位落在窗口内
    _set_next(trigger["id"], now - timedelta(minutes=25))

    results = trigger_store.fire_due(now=now)
    assert [item["status"] for item in results] == ["consumed", "skipped"]
    assert len(_runs(workflow_id)) == 1

    row = fetch_one("SELECT next_fire_at FROM triggers WHERE id = %s", (trigger["id"],))
    # 槽位被 limit 截断后，指针要推进到最后一个槽位之后（未来）
    assert row["next_fire_at"] > now


def test_catchup_skips_slots_outside_window() -> None:
    workflow_id = new_id("wf_catchup_old")
    trigger = _seed_cron(workflow_id, limit=1)
    now = datetime.now(UTC)
    # 停机 10 天：只补最近 1 小时窗口内的那个槽位，更早的丢弃
    _set_next(trigger["id"], now - timedelta(days=10))

    results = trigger_store.fire_due(now=now)
    assert [item["status"] for item in results] == ["consumed"]
    slot = datetime.fromisoformat(results[0]["scheduled_for"])
    assert timedelta(0) < now - slot <= timedelta(seconds=3600)
    assert len(_runs(workflow_id)) == 1

    row = fetch_one("SELECT next_fire_at FROM triggers WHERE id = %s", (trigger["id"],))
    assert row["next_fire_at"] > now


def test_catchup_window_zero_disables_backfill() -> None:
    workflow_id = new_id("wf_catchup_off")
    trigger = _seed_cron(workflow_id, window=0)
    now = datetime.now(UTC)
    _set_next(trigger["id"], now - timedelta(minutes=5))

    results = trigger_store.fire_due(now=now)
    assert [item["status"] for item in results] == ["skipped"]
    assert "窗口" in results[0]["detail"]
    assert _runs(workflow_id) == []

    # 关键：即使不补，指针也必须推进，否则下一轮扫描会反复处理同一时刻
    row = fetch_one("SELECT next_fire_at FROM triggers WHERE id = %s", (trigger["id"],))
    assert row["next_fire_at"] > now


def test_event_trigger_is_idempotent() -> None:
    workflow_id = new_id("wf_event")
    ensure_workflow(workflow_id, tenant_id="default", name="事件测试")
    trigger_store.project(
        workflow_id=workflow_id,
        version=1,
        tenant_id="default",
        owner_user_id="u_001",
        package_ref="pkg://test/event",
        triggers=[{"type": "event", "event_name": "order.created"}],
    )

    first = trigger_store.emit_event(
        workflow_id=workflow_id,
        event_name="order.created",
        payload={"order_id": "o-1"},
        idempotency_key="evt-1",
    )
    assert first["status"] == "consumed" and first["created"] is True

    # 同一个幂等键重投：落到同一个 run，不新增。
    # payload 故意带差异——对端重试常会补时间戳，payload 不应影响幂等
    again = trigger_store.emit_event(
        workflow_id=workflow_id,
        event_name="order.created",
        payload={"order_id": "o-1", "retried_at": "2026-11-14T10:00:00Z"},
        idempotency_key="evt-1",
    )
    assert again["run_id"] == first["run_id"]
    assert again["status"] == "skipped" and again["created"] is False
    assert len(_runs(workflow_id)) == 1

    # 没有匹配计划：ignored，不报错（避免 webhook 对端疯狂重试）
    missing = trigger_store.emit_event(workflow_id=workflow_id, event_name="order.paid")
    assert missing["status"] == "ignored"


def test_event_api_token_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    client = TestClient(create_app())
    workflow_id = new_id("wf_event_api")
    ensure_workflow(workflow_id, tenant_id="default", name="事件接口")
    trigger_store.project(
        workflow_id=workflow_id,
        version=1,
        tenant_id="default",
        owner_user_id="u_001",
        package_ref="pkg://test/event-api",
        triggers=[{"type": "event", "event_name": "ping"}],
    )
    url = f"/api/workflows/{workflow_id}/events/ping"

    # dev 且没配令牌：放行
    resp = client.post(url, json={"payload": {"k": 1}}, headers={"X-AO-Event-Id": "a1"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "consumed"

    # 配了令牌：不带/带错都拒绝，带对才通过
    monkeypatch.setattr(settings, "event_token", "s3cret")
    assert client.post(url, json={}).status_code == 401
    assert (
        client.post(url, json={}, headers={"X-AO-Event-Token": "wrong"}).status_code == 401
    )
    ok = client.post(
        url,
        json={},
        headers={"X-AO-Event-Token": "s3cret", "X-AO-Event-Id": "a2"},
    )
    assert ok.status_code == 200


def test_manifest_validates_event_and_catchup() -> None:
    with pytest.raises(ValueError, match="event_name"):
        parse({"id": "wf_ev", "nodes": [{"id": "a"}], "triggers": [{"type": "event"}]})
    with pytest.raises(ValueError, match="catchup_limit"):
        parse(
            {
                "id": "wf_ev",
                "nodes": [{"id": "a"}],
                "triggers": [{"type": "cron", "cron": "0 9 * * *", "catchup_limit": 99}],
            }
        )


# --- P2-T19：重叠策略与手动回填 ------------------------------------------------


def _seed_active_run(workflow_id: str, status: str = "running") -> str:
    """造一个未结束的 run，用来触发重叠分支。"""
    run_id, _ = create_run(
        tenant_id="default",
        workflow_id=workflow_id,
        version=1,
        package_ref="pkg://test/trigger",
        subject={"manual": True},
        window=f"manual:{new_id('w')}",
    )
    if status != "queued":
        with tx() as conn:
            conn.execute("UPDATE runs SET status = %s WHERE id = %s", (status, run_id))
    return run_id


def test_buffer_one_holds_slot_until_previous_finishes() -> None:
    workflow_id = new_id("wf_buf")
    trigger = _seed_cron(workflow_id, limit=1, policy="buffer_one")
    running_id = _seed_active_run(workflow_id)
    now = datetime.now(UTC)
    _set_next(trigger["id"], now - timedelta(minutes=2))

    first = trigger_store.fire_due(now=now)
    assert [item["status"] for item in first] == ["buffered"]
    row = fetch_one(
        "SELECT pending_fire_at, next_fire_at FROM triggers WHERE id = %s", (trigger["id"],)
    )
    assert row["pending_fire_at"] is not None
    # 缓冲不改计划指针：下一次仍按 cron 正常推进
    assert row["next_fire_at"] > now
    assert len(_runs(workflow_id)) == 1

    # 缓冲已满：后续槽位只记 skipped，不覆盖已缓冲的时刻（对齐 Temporal BUFFER_ONE）
    _set_next(trigger["id"], now - timedelta(minutes=1))
    second = trigger_store.fire_due(now=now)
    assert [item["status"] for item in second] == ["skipped"]
    assert "缓冲已满" in second[0]["detail"]

    # 上一次结束后，缓冲被拉起
    with tx() as conn:
        conn.execute("UPDATE runs SET status = 'succeeded' WHERE id = %s", (running_id,))
    third = trigger_store.fire_due(now=now + timedelta(minutes=1))
    assert [item["status"] for item in third] == ["consumed"]
    row = fetch_one("SELECT pending_fire_at FROM triggers WHERE id = %s", (trigger["id"],))
    assert row["pending_fire_at"] is None
    assert len(_runs(workflow_id)) == 2


def test_buffer_one_discards_stale_pending() -> None:
    workflow_id = new_id("wf_buf_ttl")
    trigger = _seed_cron(workflow_id, limit=1, policy="buffer_one")
    running_id = _seed_active_run(workflow_id)
    now = datetime.now(UTC)
    _set_next(trigger["id"], now - timedelta(minutes=2))
    assert [item["status"] for item in trigger_store.fire_due(now=now)] == ["buffered"]

    # 模拟 run 卡死：缓冲挂了 25 小时，超过 TTL 后必须丢弃，否则后续计划全被堵住
    with tx() as conn:
        conn.execute(
            "UPDATE triggers SET pending_since = %s WHERE id = %s",
            (now - timedelta(hours=25), trigger["id"]),
        )
        conn.execute("UPDATE runs SET status = 'succeeded' WHERE id = %s", (running_id,))
    results = trigger_store.fire_due(now=now)
    assert [item["status"] for item in results] == ["skipped"]
    row = fetch_one("SELECT pending_fire_at FROM triggers WHERE id = %s", (trigger["id"],))
    assert row["pending_fire_at"] is None
    assert len(_runs(workflow_id)) == 1


def test_cancel_other_requests_cancel_and_starts_new_run() -> None:
    workflow_id = new_id("wf_cancel")
    trigger = _seed_cron(workflow_id, limit=1, policy="cancel_other")
    running_id = _seed_active_run(workflow_id)
    now = datetime.now(UTC)
    _set_next(trigger["id"], now - timedelta(minutes=1))

    results = trigger_store.fire_due(now=now)
    assert [item["status"] for item in results] == ["consumed"]

    old = fetch_one("SELECT status, cancel_requested FROM runs WHERE id = %s", (running_id,))
    # 只置取消标志，等节点边界退出；不做强杀（DSH 单轮无 cancel）
    assert old["status"] == "running"
    assert old["cancel_requested"] is True
    assert len(_runs(workflow_id)) == 2

    audit = fetch_all(
        "SELECT status FROM trigger_events WHERE trigger_id = %s ORDER BY created_at",
        (trigger["id"],),
    )
    assert [row["status"] for row in audit] == ["cancelled", "consumed"]


def test_backfill_creates_runs_and_is_idempotent() -> None:
    workflow_id = new_id("wf_backfill")
    trigger = _seed_cron(workflow_id, cron="0 9 * * *", policy="skip")
    start = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
    end = datetime(2026, 11, 4, 0, 0, tzinfo=UTC)
    before = fetch_one("SELECT next_fire_at FROM triggers WHERE id = %s", (trigger["id"],))

    summary = trigger_store.backfill(
        workflow_id=workflow_id, trigger_id=trigger["id"], start=start, end=end
    )
    # 上海 9 点 == UTC 01:00：11/1、11/2、11/3 各一个槽位
    assert summary["planned"] == 3
    assert summary["created"] == 3
    assert len(_runs(workflow_id)) == 3

    # 重复回填命中同一幂等键：不再新建
    again = trigger_store.backfill(
        workflow_id=workflow_id, trigger_id=trigger["id"], start=start, end=end
    )
    assert again["planned"] == 3
    assert again["created"] == 0
    assert len(_runs(workflow_id)) == 3

    # 补历史不动未来计划
    after = fetch_one("SELECT next_fire_at FROM triggers WHERE id = %s", (trigger["id"],))
    assert after["next_fire_at"] == before["next_fire_at"]


def test_backfill_rejects_bad_parameters() -> None:
    workflow_id = new_id("wf_backfill_bad")
    trigger = _seed_cron(workflow_id, cron="0 9 * * *")
    start = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)

    with pytest.raises(ValueError, match="晚于"):
        trigger_store.backfill(
            workflow_id=workflow_id, trigger_id=trigger["id"], start=start, end=start
        )
    with pytest.raises(ValueError, match="31 天"):
        trigger_store.backfill(
            workflow_id=workflow_id,
            trigger_id=trigger["id"],
            start=start,
            end=start + timedelta(days=40),
        )
    # 不做静默截断：超出 limit 直接报错
    with pytest.raises(ValueError, match="超过"):
        trigger_store.backfill(
            workflow_id=workflow_id,
            trigger_id=trigger["id"],
            start=start,
            end=start + timedelta(days=3),
            limit=1,
        )
    with pytest.raises(LookupError):
        trigger_store.backfill(
            workflow_id=workflow_id, trigger_id="trg_missing", start=start, end=start
        )


def test_backfill_api_guards_and_response(monkeypatch: pytest.MonkeyPatch) -> None:
    client = TestClient(create_app())
    workflow_id = new_id("wf_backfill_api")
    trigger = _seed_cron(workflow_id, cron="0 9 * * *")
    url = f"/api/workflows/{workflow_id}/triggers/{trigger['id']}/backfill"
    body = {"start": "2026-11-01T00:00:00Z", "end": "2026-11-03T00:00:00Z"}

    ok = client.post(url, json=body)
    assert ok.status_code == 200
    assert ok.json()["planned"] == 2

    # 不带时区的时间拒绝：避免把本地时间当 UTC
    naive = client.post(
        url, json={"start": "2026-11-01T00:00:00", "end": "2026-11-03T00:00:00"}
    )
    assert naive.status_code == 400

    missing = client.post(
        f"/api/workflows/{workflow_id}/triggers/trg_missing/backfill", json=body
    )
    assert missing.status_code == 404

    monkeypatch.setattr(settings, "event_token", "s3cret")
    assert client.post(url, json=body).status_code == 401


def test_web_schedule_saves_overlap_policy(dirs: Path) -> None:
    client = TestClient(create_app())
    session_id = (
        client.get("/planner/new", follow_redirects=False)
        .headers["location"]
        .rsplit("/", 1)[-1]
    )
    client.post(f"/planner/{session_id}/messages", data={"content": "每十分钟巡检"})
    client.post(f"/planner/{session_id}/generate", follow_redirects=False)

    resp = client.post(
        f"/planner/{session_id}/schedule",
        data={
            "cron": "*/10 * * * *",
            "timezone": "Asia/Shanghai",
            "overlap_policy": "buffer_one",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    draft = draft_store.Draft.load(session_id)
    assert draft.triggers[0]["overlap_policy"] == "buffer_one"

    # 非法策略要给出错误码，而不是静默回落
    bad = __import__("asyncio").run(
        orchestrator.update_draft(
            session_id, [], schedule={"cron": "*/10 * * * *", "overlap_policy": "buffer_all"}
        )
    )
    assert bad.ok is False
    assert bad.issues[0]["code"] == "E_TRIGGER_OVERLAP"


def test_manifest_rejects_bad_overlap_policy() -> None:
    with pytest.raises(ValueError, match="overlap_policy"):
        parse(
            {
                "id": "wf_overlap",
                "nodes": [{"id": "a"}],
                "triggers": [{"type": "cron", "cron": "0 9 * * *", "overlap_policy": "buffer_all"}],
            }
        )
