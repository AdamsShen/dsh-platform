"""run 的创建与查询：幂等去重在这里，状态转移在 executor/state 里。"""

from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb

from ao.db import fetch_one, tx
from ao.util import new_id, sha256_hex, to_json


def ensure_workflow(
    workflow_id: str,
    *,
    tenant_id: str,
    name: str,
    created_by: str = "system",
    parent_id: str | None = None,
) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT INTO workflow_defs (id, tenant_id, parent_id, name, created_by)"
            " VALUES (%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING",
            (workflow_id, tenant_id, parent_id, name, created_by),
        )


def publish_version(
    workflow_id: str,
    *,
    version: int,
    manifest_hash: str,
    package_ref: str,
    status: str = "published",
    smoke_ref: str | None = None,
) -> str:
    version_id = new_id("wv")
    with tx() as conn:
        conn.execute(
            "INSERT INTO workflow_versions (id, workflow_id, version, manifest_hash,"
            " package_ref, smoke_ref, status, published_at)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s, CASE WHEN %s='published' THEN now() ELSE NULL END)"
            " ON CONFLICT (workflow_id, version) DO UPDATE SET"
            " manifest_hash=EXCLUDED.manifest_hash, package_ref=EXCLUDED.package_ref,"
            " status=EXCLUDED.status, smoke_ref=EXCLUDED.smoke_ref",
            (
                version_id,
                workflow_id,
                version,
                manifest_hash,
                package_ref,
                smoke_ref,
                status,
                status,
            ),
        )
    return version_id


def dedupe_key(workflow_id: str, version: int, subject: dict[str, Any], window: str) -> str:
    material = {"workflow": workflow_id, "version": version, "subject": subject, "window": window}
    return sha256_hex(to_json(material))


def create_run(
    *,
    tenant_id: str,
    workflow_id: str,
    version: int,
    package_ref: str,
    subject: dict[str, Any],
    window: str = "",
    trigger_user_id: str | None = None,
    budget_usd: float = 0.0,
    dedupe_key_override: str | None = None,
) -> tuple[str, bool]:
    """返回 (run_id, created)。幂等键重复时返回已有 run。

    `dedupe_key_override` 给"幂等键由外部提供"的场景用（如事件入站）：此时 subject 里的
    payload 允许不同，只要外部幂等键相同就复用同一个 run。
    """
    key = dedupe_key_override or dedupe_key(workflow_id, version, subject, window)
    run_id = new_id("run")
    with tx() as conn:
        row = conn.execute(
            "INSERT INTO runs (id, tenant_id, workflow_id, version, package_ref, dedupe_key,"
            " subject, trigger_user_id, status, budget_usd)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'queued',%s)"
            " ON CONFLICT (dedupe_key) DO NOTHING RETURNING id",
            (
                run_id,
                tenant_id,
                workflow_id,
                version,
                package_ref,
                key,
                Jsonb(subject),
                trigger_user_id,
                budget_usd,
            ),
        ).fetchone()
        if row:
            return str(row["id"]), True
    existing = fetch_one("SELECT id FROM runs WHERE dedupe_key = %s", (key,))
    if existing is None:
        raise RuntimeError("幂等键冲突但找不到已有 run")
    return str(existing["id"]), False
