"""MCP / Skill 资源目录。

绑定是**工作流级**的（`manifest.bindings`），这里负责三件事：

1. 列出当前用户可见的资源：global + 自己的 private；
2. 把绑定解析成执行器能用的配置（`mcp_servers` 字典）；
3. 发布期校验"引用存在且有权使用"，防止用别人的私有资源。

同名时私有覆盖全局：用户自己配的 server 优先于平台默认。
"""

from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb

from ao.db import fetch_all, fetch_one, tx
from ao.util import new_id


def _visible_clause() -> str:
    return "(scope = 'global' OR (scope = 'private' AND owner_user_id = %s))"


def list_available(*, user_id: str | None, kind: str | None = None) -> list[dict[str, Any]]:
    sql = (
        f"SELECT * FROM resources WHERE enabled AND {_visible_clause()}"
        " AND (%s::text IS NULL OR kind = %s)"
        " ORDER BY kind, scope DESC, ref"
    )
    rows = fetch_all(sql, (user_id, kind, kind))
    return [dict(row) for row in rows]


def get(*, kind: str, ref: str, user_id: str | None) -> dict[str, Any] | None:
    row = fetch_one(
        f"SELECT * FROM resources WHERE kind = %s AND ref = %s AND enabled"
        f" AND {_visible_clause()}"
        " ORDER BY (scope = 'private') DESC LIMIT 1",
        (kind, ref, user_id),
    )
    return dict(row) if row else None


def _binding_field(binding: Any, name: str, default: Any = None) -> Any:
    if isinstance(binding, dict):
        return binding.get(name, default)
    return getattr(binding, name, default)


def resolve_bindings(
    bindings: list[Any], *, user_id: str | None
) -> tuple[dict[str, Any], list[str], list[dict[str, Any]]]:
    """把绑定解析成 (mcp_servers, skill_refs, skipped)。

    `skipped` 只用于执行期留痕：发布期已经校验过，能走到这里说明资源在发布后被下架/删除。
    """
    servers: dict[str, Any] = {}
    skills: list[str] = []
    skipped: list[dict[str, Any]] = []
    for binding in bindings:
        kind = str(_binding_field(binding, "kind") or "")
        ref = str(_binding_field(binding, "ref") or "")
        if not kind or not ref:
            continue
        row = get(kind=kind, ref=ref, user_id=user_id)
        if row is None:
            skipped.append({"kind": kind, "ref": ref, "reason": "not_found_or_forbidden"})
            continue
        spec = row.get("spec") or {}
        if kind == "mcp":
            if isinstance(spec, dict) and spec:
                servers[ref] = dict(spec)
            else:
                skipped.append({"kind": kind, "ref": ref, "reason": "empty_spec"})
        else:
            skills.append(ref)
    return servers, skills, skipped


def check_bindings(manifest: Any, *, user_id: str | None) -> list[dict[str, Any]]:
    """发布期校验：每个绑定都必须可解析，并说明失败原因。"""
    issues: list[dict[str, Any]] = []
    for binding in manifest.effective_bindings():
        kind, ref = binding.kind, binding.ref
        row = get(kind=kind, ref=ref, user_id=user_id)
        if row is not None:
            continue
        any_row = fetch_one(
            "SELECT scope, owner_user_id, enabled FROM resources WHERE kind = %s AND ref = %s"
            " ORDER BY (scope = 'private') DESC LIMIT 1",
            (kind, ref),
        )
        if any_row is None:
            issues.append(
                {
                    "code": "E_BINDING_MISSING",
                    "message": f"{kind} 绑定不存在: {ref}（先在工作流设置里选择可用资源）",
                }
            )
        elif not any_row["enabled"]:
            issues.append(
                {"code": "E_BINDING_DISABLED", "message": f"{kind} 已下架: {ref}"}
            )
        else:
            issues.append(
                {
                    "code": "E_BINDING_FORBIDDEN",
                    "message": f"{kind} 是他人私有资源，不能引用: {ref}",
                }
            )
    return issues


def upsert(
    *,
    kind: str,
    ref: str,
    name: str,
    spec: dict[str, Any] | None = None,
    description: str = "",
    scope: str = "global",
    owner_user_id: str | None = None,
    tenant_id: str | None = None,
    enabled: bool = True,
) -> dict[str, Any]:
    """注册或更新资源；同 (kind, ref, scope, owner) 覆盖。"""
    if kind not in {"mcp", "skill"}:
        raise ValueError(f"kind 必须是 mcp 或 skill: {kind}")
    if scope not in {"global", "private"}:
        raise ValueError(f"scope 必须是 global 或 private: {scope}")
    if scope == "private" and not owner_user_id:
        raise ValueError("私有资源必须指定 owner_user_id")
    with tx() as conn:
        existing = conn.execute(
            "SELECT id FROM resources WHERE kind = %s AND ref = %s AND scope = %s"
            " AND owner_user_id IS NOT DISTINCT FROM %s",
            (kind, ref, scope, owner_user_id),
        ).fetchone()
        if existing:
            row = conn.execute(
                "UPDATE resources SET name = %s, description = %s, spec = %s,"
                " tenant_id = %s, enabled = %s WHERE id = %s RETURNING *",
                (name, description, Jsonb(spec or {}), tenant_id, enabled, existing["id"]),
            ).fetchone()
        else:
            row = conn.execute(
                "INSERT INTO resources (id, kind, scope, owner_user_id, tenant_id, ref, name,"
                " description, spec, enabled) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                " RETURNING *",
                (
                    new_id("res"),
                    kind,
                    scope,
                    owner_user_id,
                    tenant_id,
                    ref,
                    name,
                    description,
                    Jsonb(spec or {}),
                    enabled,
                ),
            ).fetchone()
    return dict(row)
