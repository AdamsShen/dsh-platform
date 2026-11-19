"""W30：用户维度费用——同一批里 A/B 能分开查，合计等于总额。"""

from __future__ import annotations

from ao import usage
from ao.db import fetch_one, tx
from ao.runs import create_run, ensure_workflow
from ao.util import new_id


def _run(user_id: str) -> str:
    workflow_id = new_id("wf_usage")
    ensure_workflow(workflow_id, tenant_id="default", name="费用测试")
    run_id, _ = create_run(
        tenant_id="default",
        workflow_id=workflow_id,
        version=1,
        package_ref="pkg://test/usage",
        subject={"user_id": user_id},
        window=f"usage#{user_id}",
        trigger_user_id=user_id,
    )
    return run_id


def test_summary_by_user_splits_and_reconciles() -> None:
    run_a = _run("user-A")
    run_b = _run("user-B")
    with tx() as conn:
        usage.record(
            conn,
            run_id=run_a,
            node_id="draft",
            attempt=0,
            usage={"input_tokens": 1000, "output_tokens": 0, "model": "m"},
            user_id="user-A",
        )
        usage.record(
            conn,
            run_id=run_b,
            node_id="draft",
            attempt=0,
            usage={"input_tokens": 3000, "output_tokens": 0, "model": "m"},
            user_id="user-B",
        )
        usage.refresh_run_spend(conn, run_a)
        usage.refresh_run_spend(conn, run_b)

    rows = {row["user_id"]: float(row["cost"]) for row in usage.summary_by_user()}
    assert set(rows) == {"user-A", "user-B"}
    assert rows["user-B"] > rows["user-A"]

    total_row = fetch_one(
        "SELECT COALESCE(SUM(cost), 0) AS total FROM llm_usage WHERE run_id = ANY(%s)",
        ([run_a, run_b],),
    )
    total = float(total_row["total"])
    assert abs(sum(rows.values()) - total) < 1e-9


def test_authoring_usage_has_no_user_dimension() -> None:
    with tx() as conn:
        usage.record(
            conn,
            run_id=None,
            node_id=None,
            attempt=0,
            usage={"input_tokens": 500, "output_tokens": 10, "model": "m"},
            purpose="authoring",
        )
    assert usage.summary_by_user() == [] or all(
        row["user_id"] for row in usage.summary_by_user()
    )
