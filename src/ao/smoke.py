"""发布前冒烟：用 mock provider 把整图跑一遍。

不是单元测试的替代：它验证的是"这个包在平台里能不能从头走到尾"——
节点齐全、契约能过、产物能落、事件能写。失败按阶段返回，不发布。
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from ao.db import fetch_all, tx
from ao.executor import execute
from ao.manifest import parse
from ao.package import load_package
from ao.runs import create_run, ensure_workflow, publish_version
from ao.settings import settings


async def run_smoke(
    package_root: Path,
    *,
    subject: dict[str, Any] | None = None,
    timeout: float = 300.0,
) -> dict[str, Any]:
    package = load_package(package_root, verify=False)
    manifest = parse(package.manifest)
    tenant = manifest.tenant or settings.default_tenant

    with tx() as conn:
        conn.execute(
            "INSERT INTO tenants (id, name) VALUES (%s,%s) ON CONFLICT DO NOTHING",
            (tenant, tenant),
        )
    ensure_workflow(manifest.id, tenant_id=tenant, name=manifest.name or manifest.id)
    publish_version(
        manifest.id,
        version=manifest.version,
        manifest_hash=package.manifest_hash,
        package_ref=str(package.root),
        status="draft",
    )

    run_id, _ = create_run(
        tenant_id=tenant,
        workflow_id=manifest.id,
        version=manifest.version,
        package_ref=str(package.root),
        subject=subject or {"user_id": "u_001", "name": "冒烟"},
        window=f"smoke-{uuid.uuid4().hex[:10]}",
    )

    previous_mock = settings.mock
    settings.mock = True  # 冒烟必须确定性，不能真调模型
    try:
        status = await execute(run_id)
    finally:
        settings.mock = previous_mock

    nodes = fetch_all(
        "SELECT node_id, status, error FROM run_nodes WHERE run_id = %s ORDER BY started_at",
        (run_id,),
    )
    run = fetch_all("SELECT status, error FROM runs WHERE id = %s", (run_id,))[0]

    ok = status == "succeeded" and all(node["status"] == "succeeded" for node in nodes)
    result = {
        "ok": ok,
        "status": run["status"],
        "error": run["error"],
        "run_id": run_id,
        "nodes": nodes,
        "expected_nodes": [node.id for node in manifest.nodes],
    }

    if ok:
        # 成功的冒烟 run 不留痕，避免污染成本报表；失败保留用于排查
        with tx() as conn:
            conn.execute("DELETE FROM llm_usage WHERE run_id = %s", (run_id,))
            conn.execute("DELETE FROM runs WHERE id = %s", (run_id,))
    return result
