"""扇出演练（P5-T08）：不跑模型，只验证"到点 → 按主体建 run"这一段。

用法：
    uv run python scripts/drill_fanout.py          # 10 个主体
    uv run python scripts/drill_fanout.py 200      # 200 个主体（看展开耗时）

覆盖场景（与 docs/plans/2026-11-19-multi-user-fanout.md T08 对齐）：
1. tenant_active 展开 → 每个主体一个 run，subject.user_id 互不相同；
2. 同一时刻重复投递 → 不新增 run；
3. 单主体卡住 → 其他人照常建；
4. 超过 fanout_limit → 整批拒绝、0 个 run；
5. 统计展开与建 run 耗时，用于估算 1000 主体的批量成本。
"""

from __future__ import annotations

import sys
import time
from datetime import UTC, datetime, timedelta

from ao import triggers as trigger_store
from ao import users as users_store
from ao.db import close_pool, fetch_all, tx
from ao.migrate import migrate
from ao.runs import ensure_workflow
from ao.settings import settings
from ao.util import new_id

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}{(' ' + detail) if detail else ''}")


def _seed(tenant: str, count: int) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT INTO tenants (id, name) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (tenant, tenant),
        )
    for index in range(count):
        users_store.upsert(
            tenant_id=tenant, external_ref=f"drill-{index:04d}", display_name=f"演练{index}"
        )


def _project(tenant: str, *, limit: int) -> tuple[str, dict]:
    workflow_id = new_id("wf_drill")
    ensure_workflow(workflow_id, tenant_id=tenant, name="扇出演练")
    rows = trigger_store.project(
        workflow_id=workflow_id,
        version=1,
        tenant_id=tenant,
        owner_user_id="u_drill",
        package_ref="pkg://drill/fanout",
        triggers=[
            {
                "type": "cron",
                "cron": "0 9 * * *",
                "timezone": "UTC",
                "fanout": {"mode": "tenant_active", "limit": limit},
            }
        ],
    )
    return workflow_id, rows[0]


def _fire(trigger_id: str, slot: datetime) -> list[dict]:
    with tx() as conn:
        conn.execute("UPDATE triggers SET next_fire_at = %s WHERE id = %s", (slot, trigger_id))
    return trigger_store.fire_due(now=datetime.now(UTC))


def _runs(workflow_id: str) -> list[dict]:
    return fetch_all("SELECT * FROM runs WHERE workflow_id = %s", (workflow_id,))


def main() -> int:
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    migrate()
    tenant = new_id("drill")
    _seed(tenant, count)
    slot = datetime.now(UTC) - timedelta(minutes=1)

    workflow_id, trigger = _project(tenant, limit=max(count, 1))
    started = time.perf_counter()
    first = _fire(trigger["id"], slot)
    elapsed = time.perf_counter() - started
    runs = _runs(workflow_id)
    check(
        f"{count} 个主体各建一个 run",
        len(runs) == count and first[0]["created"] == count,
        f"runs={len(runs)} 耗时={elapsed:.2f}s",
    )
    uids = {row["subject"].get("user_id") for row in runs}
    check("subject.user_id 互不相同", len(uids) == count, f"unique={len(uids)}")

    started = time.perf_counter()
    second = _fire(trigger["id"], slot)
    check(
        "同一时刻重复投递不新增",
        len(_runs(workflow_id)) == count and second[0]["created"] == 0,
        f"created={second[0]['created']} skipped={second[0]['skipped']} "
        f"耗时={time.perf_counter() - started:.2f}s",
    )

    # 单主体卡住：只留最早那个 run 在 queued，其余标成功，再换一个槽位应只跳过它
    later = slot + timedelta(minutes=1)
    with tx() as conn:
        conn.execute(
            "UPDATE runs SET status = 'succeeded' WHERE workflow_id = %s", (workflow_id,)
        )
        conn.execute(
            "UPDATE runs SET status = 'queued' WHERE id ="
            " (SELECT id FROM runs WHERE workflow_id = %s ORDER BY created_at LIMIT 1)",
            (workflow_id,),
        )
    blocked = _fire(trigger["id"], later)
    check(
        "单主体卡住不阻塞他人",
        blocked[0]["skipped"] == 1 and blocked[0]["created"] == count - 1,
        f"skipped={blocked[0]['skipped']} created={blocked[0]['created']}",
    )

    # 超限整批拒绝
    limited_wf, limited = _project(tenant, limit=max(count - 1, 1))
    rejected = _fire(limited["id"], slot)
    check(
        "超过 fanout_limit 整批拒绝",
        rejected[0]["status"] == "failed" and _runs(limited_wf) == [],
        rejected[0].get("detail", ""),
    )

    close_pool()
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} 场景通过")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
