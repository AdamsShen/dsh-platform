"""免登录分享（P4-T10 / M4a-W48）。

分工：
- 本模块只管"这个 token 还能不能用、能看到什么范围"；
- 页面与静态资源复用 `ao.pages.hosting`，数据复用 `ao.pages.datasources`；
- 动作在分享态一律 403：免登录链接不能成为改数据的入口。

token 只存 sha256：库被读走也拿不到能用的链接；scope 落 JSONB，但**取数时编译成 SQL
谓词**而不是取回再过滤——过滤太靠后，一旦有人忘了过滤就会越权。
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg.types.json import Jsonb

from ao.db import fetch_one, tx
from ao.util import new_id, sha256_hex

#: scope 允许出现的键：多一个都不认，避免"写了个没人实现的过滤条件"造成假安全
SCOPE_KEYS = frozenset({"workflow_id", "status", "since"})
DEFAULT_TTL_HOURS = 72
MAX_TTL_HOURS = 24 * 30


class ShareError(RuntimeError):
    """分享链接不可用；`code` 直接映射 HTTP：missing/revoked/expired/exhausted。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def create(
    *,
    tenant_id: str,
    workflow_id: str,
    version: int,
    page_id: str,
    scope: dict[str, Any] | None = None,
    ttl_hours: int = DEFAULT_TTL_HOURS,
    max_uses: int | None = None,
    created_by: str,
) -> tuple[str, str, datetime]:
    """建链接；返回 (share_id, 明文 token, 过期时间)。明文只在这一刻存在。"""
    scope = dict(scope or {})
    unknown = set(scope) - SCOPE_KEYS
    if unknown:
        raise ValueError(f"scope 不支持的键: {sorted(unknown)}")
    if scope.get("workflow_id") and scope["workflow_id"] != workflow_id:
        # 分享的是这个工作流的页面，scope 却指到别处：多半是调用方写错了
        raise ValueError("scope.workflow_id 与分享的 workflow_id 不一致")
    ttl = max(1, min(MAX_TTL_HOURS, int(ttl_hours)))
    expires_at = datetime.now(UTC) + timedelta(hours=ttl)
    token = "s_" + secrets.token_urlsafe(24)
    share_id = new_id("share")
    with tx() as conn:
        conn.execute(
            "INSERT INTO share_links (id, tenant_id, workflow_id, version, page_id, scope,"
            " token_hash, expires_at, max_uses, created_by)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                share_id,
                tenant_id,
                workflow_id,
                int(version),
                page_id,
                Jsonb(scope),
                sha256_hex(token),
                expires_at,
                max_uses,
                created_by,
            ),
        )
    return share_id, token, expires_at


def revoke(share_id: str) -> bool:
    with tx() as conn:
        updated = conn.execute(
            "UPDATE share_links SET revoked_at = now()"
            " WHERE id = %s AND revoked_at IS NULL",
            (share_id,),
        )
        return updated.rowcount > 0


def resolve(token: str) -> dict[str, Any]:
    """校验 token；不可用时抛带 code 的 `ShareError`，路由层翻成 404/429。"""
    if not token:
        raise ShareError("missing", "分享链接不存在")
    row = fetch_one(
        "SELECT * FROM share_links WHERE token_hash = %s", (sha256_hex(token),)
    )
    if row is None:
        raise ShareError("missing", "分享链接不存在")
    if row["revoked_at"] is not None:
        raise ShareError("revoked", "分享链接已撤销")
    if row["expires_at"] <= datetime.now(UTC):
        raise ShareError("expired", "分享链接已过期")
    if row["max_uses"] is not None and row["used_count"] >= int(row["max_uses"]):
        raise ShareError("exhausted", "分享链接使用次数已用尽")
    return dict(row)


def touch(share: dict[str, Any], *, status: int, in_scope: bool = True) -> None:
    """记一次访问并累加计数；超限由下一轮 `resolve` 拦，不做行锁长事务。"""
    with tx() as conn:
        conn.execute(
            "INSERT INTO share_access (share_id, page_id, status, in_scope)"
            " VALUES (%s,%s,%s,%s)",
            (share["id"], share["page_id"], status, in_scope),
        )
        conn.execute(
            "UPDATE share_links SET used_count = used_count + 1 WHERE id = %s",
            (share["id"],),
        )


def scope_sql(scope: dict[str, Any], *, default_workflow_id: str) -> tuple[str, list[Any]]:
    """把 scope 编译成 runs 表谓词；未声明 workflow_id 时锁到分享的工作流。"""
    clauses = ["workflow_id = %s"]
    values: list[Any] = [str(scope.get("workflow_id") or default_workflow_id)]
    status = scope.get("status")
    if isinstance(status, str) and status:
        clauses.append("status = %s")
        values.append(status)
    since = scope.get("since")
    if isinstance(since, str) and since:
        clauses.append("created_at >= %s")
        values.append(since)
    return " AND ".join(clauses), values


def row_in_scope(scope: dict[str, Any], row: dict[str, Any], *, workflow_id: str) -> bool:
    """单对象越权判定：run.get/events 取回后再核一次，不在范围就按不存在处理。"""
    if str(row.get("workflow_id") or "") != str(scope.get("workflow_id") or workflow_id):
        return False
    status = scope.get("status")
    if isinstance(status, str) and status and row.get("status") != status:
        return False
    return True
