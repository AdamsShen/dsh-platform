from __future__ import annotations

import pytest

from ao.db import fetch_all, fetch_one, tx
from ao.executor import execute
from ao.package import freeze_package, load_package
from ao.runs import create_run, ensure_workflow, publish_version
from ao.settings import PROJECT_ROOT

EXAMPLE = PROJECT_ROOT / "examples" / "daily_schedule"


def publish(subject: dict, window: str) -> str:
    manifest_hash, _ = freeze_package(EXAMPLE)
    manifest = load_package(EXAMPLE).manifest
    with tx() as conn:
        conn.execute(
            "INSERT INTO tenants (id, name) VALUES ('default','default') ON CONFLICT DO NOTHING"
        )
    ensure_workflow(manifest["id"], tenant_id="default", name=manifest["name"])
    publish_version(
        manifest["id"],
        version=manifest["version"],
        manifest_hash=manifest_hash,
        package_ref=str(EXAMPLE),
    )
    run_id, _ = create_run(
        tenant_id="default",
        workflow_id=manifest["id"],
        version=manifest["version"],
        package_ref=str(EXAMPLE),
        subject=subject,
        window=window,
    )
    return run_id


@pytest.mark.integration
async def test_run_succeeds_and_bills() -> None:
    run_id = publish({"user_id": "u_001", "name": "小林"}, "w1")
    assert await execute(run_id) == "succeeded"

    run = fetch_one("SELECT status, spent_usd FROM runs WHERE id = %s", (run_id,))
    assert run["status"] == "succeeded"
    assert float(run["spent_usd"]) > 0

    nodes = fetch_all(
        "SELECT node_id, status, output, native_id, repair_count FROM run_nodes"
        " WHERE run_id = %s ORDER BY started_at",
        (run_id,),
    )
    assert [n["node_id"] for n in nodes] == ["draft", "finalize"]
    assert all(n["status"] == "succeeded" for n in nodes)
    assert nodes[0]["output"]["plan_path"] == "mock-plan_path"
    assert nodes[0]["native_id"].startswith("mock-")

    usage_rows = fetch_all("SELECT cost FROM llm_usage WHERE run_id = %s", (run_id,))
    assert len(usage_rows) == 2
    total = sum(float(row["cost"]) for row in usage_rows)
    assert abs(total - float(run["spent_usd"])) < 1e-9

    types = [
        e["type"]
        for e in fetch_all("SELECT type FROM run_events WHERE run_id = %s ORDER BY seq", (run_id,))
    ]
    assert types[0] == "var.resolved"  # run 级变量先解析
    assert "node.started" in types
    assert types[-1] == "run.finished"


@pytest.mark.integration
async def test_contract_failure_repairs_once_then_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AO_FAIL_NODE", "draft")
    run_id = publish({"user_id": "u_002", "name": "阿岚"}, "w2")

    assert await execute(run_id) == "failed"

    node = fetch_one(
        "SELECT status, repair_count, error FROM run_nodes"
        " WHERE run_id = %s AND node_id = 'draft'",
        (run_id,),
    )
    assert node["status"] == "failed"
    assert node["repair_count"] == 1
    assert "contract_error" in node["error"]

    repairs = fetch_all(
        "SELECT payload FROM run_events WHERE run_id = %s AND type = 'node.repair'", (run_id,)
    )
    assert len(repairs) == 1


@pytest.mark.integration
async def test_fencing_blocks_stale_epoch() -> None:
    from ao.executor import FencedError, RunRuntime
    from ao.harness.mock import MockProvider
    from ao.manifest import parse

    run_id = publish({"user_id": "u_003", "name": "老陈"}, "w3")
    with tx() as conn:
        conn.execute("UPDATE runs SET epoch = epoch + 1 WHERE id = %s", (run_id,))

    run = fetch_one("SELECT * FROM runs WHERE id = %s", (run_id,))
    package = load_package(EXAMPLE)
    runtime = RunRuntime(
        run={**run, "epoch": 0},  # 模拟"上一个 owner 还活着"
        manifest=parse(package.manifest),
        package=package,
        provider=MockProvider(),
    )
    with pytest.raises(FencedError):
        runtime.check_cancel()


@pytest.mark.integration
async def test_vars_are_resolved_and_cached() -> None:
    run_id = publish({"user_id": "u_001", "name": "小林"}, "w4")
    assert await execute(run_id) == "succeeded"

    rows = fetch_all(
        "SELECT node_id, name, source FROM run_vars WHERE run_id = %s ORDER BY name", (run_id,)
    )
    names = {(row["node_id"], row["name"]) for row in rows}
    assert ("", "today") in names
    assert ("draft", "role") in names
    assert ("draft", "mood") in names
    assert all(row["source"] == "mcp" for row in rows)

    resolved = fetch_all(
        "SELECT payload FROM run_events WHERE run_id = %s AND type = 'var.resolved'", (run_id,)
    )
    assert len(resolved) == 3
    tool_calls = [
        e["payload"]
        for e in fetch_all(
            "SELECT payload FROM run_events WHERE run_id = %s AND type = 'tool.call'", (run_id,)
        )
    ]
    assert any("clock__today" in str(item.get("tool")) for item in tool_calls)


@pytest.mark.integration
async def test_budget_exhausted_pauses_and_resumes() -> None:
    from ao.state import resume

    run_id = publish({"user_id": "u_001", "name": "小林"}, "w6")
    with tx() as conn:
        conn.execute("UPDATE runs SET budget_usd = 0.000001 WHERE id = %s", (run_id,))

    assert await execute(run_id) == "paused_budget"
    run = fetch_one("SELECT status, spent_usd FROM runs WHERE id = %s", (run_id,))
    assert run["status"] == "paused_budget"
    assert float(run["spent_usd"]) > 0

    assert resume(run_id) is True
    row = fetch_one("SELECT status, epoch FROM runs WHERE id = %s", (run_id,))
    assert row["status"] == "queued"
    assert row["epoch"] == 1


@pytest.mark.integration
async def test_missing_var_retries_then_fails() -> None:
    run_id = publish({"user_id": "u_999", "name": "陌生人"}, "w5")
    assert await execute(run_id) == "queued"

    # 第二次执行时 today 已缓存，不应再次请求 clock.today
    assert await execute(run_id) == "failed"
    run = fetch_one("SELECT status, retry_count FROM runs WHERE id = %s", (run_id,))
    assert run["status"] == "failed"
    assert run["retry_count"] == 1

    today_resolved = fetch_all(
        "SELECT payload FROM run_events WHERE run_id = %s AND type = 'var.resolved'"
        " AND payload->>'var' = 'today'",
        (run_id,),
    )
    # 第一次真解析，第二次命中缓存（两条事件，其中一条 cached=true）
    assert len(today_resolved) == 2
    assert any(item["payload"].get("cached") for item in today_resolved)
    today_tool_calls = fetch_all(
        "SELECT payload FROM run_events WHERE run_id = %s AND type = 'tool.call'"
        " AND payload->>'var' = 'today'",
        (run_id,),
    )
    assert len(today_tool_calls) == 1

    failed = fetch_all(
        "SELECT payload FROM run_events WHERE run_id = %s AND type = 'var.failed'", (run_id,)
    )
    assert failed
    assert any("u_999" in str(item["payload"]) for item in failed)
