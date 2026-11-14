"""P1-T18 验收演练：把"正常 / 契约失败 / 取数失败 / 停止 / 接管 / 预算"跑一遍。

用 mock provider（确定性、不花钱），验证的是状态机与队列的**行为**，不是模型质量。
真跑版本：`AO_MOCK=0 ... scripts/drill.py`（需要 key；没有网关时账单为空）。

用法：
    uv run python scripts/drill.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

from ao.db import close_pool, fetch_one, tx
from ao.executor import execute
from ao.migrate import migrate
from ao.package import freeze_package, load_package
from ao.queue import claim_next, reclaim_expired
from ao.runs import create_run, ensure_workflow, publish_version
from ao.settings import PROJECT_ROOT, settings
from ao.state import request_cancel

EXAMPLE = PROJECT_ROOT / "examples" / "daily_schedule"
RESULTS: list[tuple[str, str, str, bool]] = []


def prepare() -> dict:
    manifest_hash, _ = freeze_package(EXAMPLE)
    package = load_package(EXAMPLE)
    manifest = package.manifest
    with tx() as conn:
        conn.execute(
            "INSERT INTO tenants (id, name) VALUES ('default','default') ON CONFLICT DO NOTHING"
        )
    ensure_workflow(
        manifest["id"], tenant_id="default", name=manifest.get("name") or manifest["id"]
    )
    publish_version(
        manifest["id"],
        version=int(manifest["version"]),
        manifest_hash=manifest_hash,
        package_ref=str(EXAMPLE),
    )
    return manifest


def new_run(manifest: dict, tag: str, subject: dict | None = None) -> str:
    run_id, _ = create_run(
        tenant_id="default",
        workflow_id=manifest["id"],
        version=int(manifest["version"]),
        package_ref=str(EXAMPLE),
        subject=subject or {"user_id": "u_001", "name": "小林"},
        window=f"drill-{tag}-{int(time.time() * 1000)}",
    )
    return run_id


def reset_runs() -> None:
    """演练库里的历史 queued/running 会干扰全局队列，先清场（仅 dev）。"""
    if settings.env != "dev":
        raise SystemExit("drill.py 只允许在 AO_ENV=dev 下运行")
    with tx() as conn:
        conn.execute(
            "UPDATE runs SET status='cancelled', lease_owner=NULL, lease_until=NULL"
            " WHERE status IN ('queued','running')"
        )


def record(name: str, expected: str, actual: str) -> None:
    RESULTS.append((name, expected, actual, expected == actual))
    print(
        f"[{'PASS' if expected == actual else 'FAIL'}] {name}:"
        f" expected={expected} actual={actual}"
    )


async def scenario_happy(manifest: dict) -> None:
    run_id = new_run(manifest, "happy")
    record("正常跑通", "succeeded", await execute(run_id))


async def scenario_contract_fail(manifest: dict) -> None:
    os.environ["AO_FAIL_NODE"] = "draft"
    try:
        run_id = new_run(manifest, "contract")
        actual = await execute(run_id)
    finally:
        os.environ.pop("AO_FAIL_NODE", None)
    record("契约失败（repair 后仍不合格）", "failed", actual)


async def scenario_missing_var(manifest: dict) -> None:
    run_id = new_run(manifest, "missing", subject={"user_id": "u_999", "name": "陌生人"})
    # missing_var 允许一次性重试：第一次回队列，第二次才终态失败
    first = await execute(run_id)
    second = await execute(run_id)
    actual = f"{first}->{second}"
    record("变量取数失败（一次重试后终态）", "queued->failed", actual)


async def scenario_cancel(manifest: dict) -> None:
    run_id = new_run(manifest, "cancel")
    request_cancel(run_id, actor="drill")
    record("排队中停止", "cancelled", await execute(run_id))


async def scenario_takeover(manifest: dict) -> None:
    run_id = new_run(manifest, "takeover")
    claimed = claim_next("drill-a")
    if claimed is None or str(claimed["id"]) != run_id:
        record("过期租约接管", "succeeded", f"claimed_wrong_run:{claimed and claimed['id']}")
        return
    with tx() as conn:
        conn.execute(
            "UPDATE runs SET lease_until = now() - interval '1 second' WHERE id = %s", (run_id,)
        )
    reclaimed = reclaim_expired()
    again = claim_next("drill-b")
    epoch = int(fetch_one("SELECT epoch FROM runs WHERE id = %s", (run_id,))["epoch"])
    actual = await execute(run_id) if again else "claim_failed"
    # 接管必须换 epoch，否则旧 owner 还能写
    if actual == "succeeded" and epoch != 1:
        actual = f"succeeded_but_epoch_{epoch}"
    record("过期租约接管", "succeeded", actual)
    assert reclaimed, "reclaim_expired 没有回收任何 run"


async def scenario_budget(manifest: dict) -> None:
    run_id = new_run(manifest, "budget")
    with tx() as conn:
        conn.execute("UPDATE runs SET budget_usd = 0.000001 WHERE id = %s", (run_id,))
    record("预算耗尽暂停", "paused_budget", await execute(run_id))


async def main() -> int:
    migrate()
    reset_runs()
    manifest = prepare()
    print(f"mock={settings.mock} model={settings.dsh_model or 'default'}")
    for scenario in (
        scenario_happy,
        scenario_contract_fail,
        scenario_missing_var,
        scenario_cancel,
        scenario_takeover,
        scenario_budget,
    ):
        try:
            await scenario(manifest)
        except Exception as exc:  # noqa: BLE001 — 演练要跑完全部场景
            record(scenario.__name__, "no-exception", f"{type(exc).__name__}: {exc}")

    failed = [row for row in RESULTS if not row[3]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} 场景通过")
    close_pool()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
