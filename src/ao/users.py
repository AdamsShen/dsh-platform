"""引擎侧主体目录（P5-T01）。

只回答"某租户下哪些主体可以被扇出"，**不承担登录/鉴权**——那是 P3-T02 的事。
`users` 表在 001 就建好了（带 password_hash/role），这里不引入账号体系：
写目录时 password_hash 落空串、role 统一 operator，表示"这条记录只用于展开与追溯"。

用法：
    upsert(...)            # 业务侧同步主体（或 scripts/seed_users.py 造测试数据）
    list_active_users()    # 触发器 fanout_mode='tenant_active' 时取名单
"""

from __future__ import annotations

from typing import Any

from ao.db import fetch_all, fetch_one, tx
from ao.settings import settings
from ao.util import new_id


class UserError(ValueError):
    """主体目录参数不合法（缺 external_ref、状态非法等）。"""


_ALLOWED_STATUS = {"pending", "active", "rejected"}


def upsert(
    *,
    tenant_id: str | None = None,
    external_ref: str,
    display_name: str | None = None,
    status: str = "active",
    user_id: str | None = None,
) -> dict[str, Any]:
    """按 (tenant_id, external_ref) 幂等写入一个主体，返回落库后的行。

    `external_ref` 是业务主键（如角色 id / 员工号），必须非空——为空时无法作为
    扇出主体的稳定标识，会让"同一用户每天生成两份"变成可能。
    """
    tenant = tenant_id or settings.default_tenant
    ref = (external_ref or "").strip()
    if not ref:
        raise UserError("external_ref 不能为空")
    if status not in _ALLOWED_STATUS:
        raise UserError(f"status 只支持 pending/active/rejected: {status!r}")

    with tx() as conn:
        existing = conn.execute(
            "SELECT id FROM users WHERE tenant_id = %s AND external_ref = %s",
            (tenant, ref),
        ).fetchone()
        if existing:
            row = conn.execute(
                "UPDATE users SET display_name = COALESCE(%s, display_name), status = %s"
                " WHERE id = %s RETURNING *",
                (display_name, status, existing["id"]),
            ).fetchone()
        else:
            row = conn.execute(
                "INSERT INTO users (id, tenant_id, username, display_name, role, status,"
                " password_hash, external_ref)"
                " VALUES (%s,%s,%s,%s,'operator',%s,'',%s) RETURNING *",
                (
                    user_id or new_id("usr"),
                    tenant,
                    f"{tenant}:{ref}",
                    display_name or ref,
                    status,
                    ref,
                ),
            ).fetchone()
    return dict(row)


def list_active_users(tenant_id: str | None = None) -> list[dict[str, Any]]:
    """列出该租户可被扇出的主体（status='active'），按 external_ref 稳定排序。

    返回字段只保留展开与追溯需要的：id / external_ref / display_name。
    没有 external_ref 的历史行用平台 id 兜底，保证扇出不丢人。
    """
    tenant = tenant_id or settings.default_tenant
    rows = fetch_all(
        "SELECT id, external_ref, display_name FROM users"
        " WHERE tenant_id = %s AND status = 'active'"
        " ORDER BY COALESCE(external_ref, id)",
        (tenant,),
    )
    return [dict(row) for row in rows]


def resolve(tenant_id: str | None, refs: list[str]) -> list[dict[str, Any]]:
    """把静态名单（external_ref 或平台 id）解析成主体行；不存在的跳过。

    返回顺序与入参一致，便于审计里对照"谁没解析出来"。
    """
    tenant = tenant_id or settings.default_tenant
    wanted = [str(ref).strip() for ref in refs if str(ref).strip()]
    if not wanted:
        return []
    rows = fetch_all(
        "SELECT id, external_ref, display_name FROM users"
        " WHERE tenant_id = %s AND status = 'active'"
        " AND (external_ref = ANY(%s) OR id = ANY(%s))",
        (tenant, wanted, wanted),
    )
    by_ref: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = dict(row)
        by_ref[str(item["id"])] = item
        if item.get("external_ref"):
            by_ref[str(item["external_ref"])] = item
    return [by_ref[ref] for ref in wanted if ref in by_ref]


def get(user_id: str) -> dict[str, Any] | None:
    row = fetch_one("SELECT * FROM users WHERE id = %s", (user_id,))
    return dict(row) if row else None
