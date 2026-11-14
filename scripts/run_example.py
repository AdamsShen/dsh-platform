"""P1 尖刀：跑通"变量 → 节点 turn → 契约 → 事件 → 记账"。

用法：
    uv run python scripts/run_example.py             # mock provider
    AO_MOCK=0 uv run python scripts/run_example.py   # 真实 DSH（需 SDK 与 key）
"""

from __future__ import annotations

import asyncio
import sys
import time

from ao.db import close_pool, fetch_all, fetch_one, tx
from ao.executor import execute
from ao.migrate import migrate
from ao.package import freeze_package, load_package
from ao.runs import create_run, ensure_workflow, publish_version
from ao.settings import PROJECT_ROOT, settings

EXAMPLES = PROJECT_ROOT / "examples"


def seed_tenant(tenant_id: str = "default") -> None:
    with tx() as conn:
        conn.execute(
            "INSERT INTO tenants (id, name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
            (tenant_id, tenant_id),
        )


def publish_example(name: str = "daily_schedule") -> tuple[str, int, str]:
    root = EXAMPLES / name
    manifest_hash, _ = freeze_package(root)
    package = load_package(root)
    manifest = package.manifest
    ensure_workflow(
        manifest["id"],
        tenant_id=manifest.get("tenant", "default"),
        name=manifest.get("name") or manifest["id"],
    )
    publish_version(
        manifest["id"],
        version=int(manifest["version"]),
        manifest_hash=manifest_hash,
        package_ref=str(root),
    )
    return manifest["id"], int(manifest["version"]), str(root)


async def main() -> int:
    migrate()
    seed_tenant()
    workflow_id, version, package_ref = publish_example()

    # 默认用时间戳做 window，避免重复执行时被 dedupe 命中旧 run
    window = sys.argv[1] if len(sys.argv) > 1 else f"manual-{int(time.time())}"
    run_id, created = create_run(
        tenant_id="default",
        workflow_id=workflow_id,
        version=version,
        package_ref=package_ref,
        subject={"user_id": "u_001", "name": "小林"},
        window=window,
    )
    print(f"run={run_id} created={created} mock={settings.mock}")

    status = await execute(run_id)
    run = fetch_one("SELECT status, spent_usd, error FROM runs WHERE id = %s", (run_id,))
    print(
        f"status={status} db_status={run['status']}"
        f" spent={float(run['spent_usd']):.6f} error={run['error']}"
    )

    usage_rows = fetch_all(
        "SELECT node_id, model, input_tokens, output_tokens, cost"
        " FROM llm_usage WHERE run_id = %s ORDER BY id",
        (run_id,),
    )
    for row in usage_rows:
        print("usage:", row)

    events = fetch_all(
        "SELECT type, node_id, payload FROM run_events WHERE run_id = %s ORDER BY seq", (run_id,)
    )
    for event in events:
        print("event:", event["type"], event["node_id"] or "", event["payload"])

    close_pool()
    return 0 if status == "succeeded" else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
