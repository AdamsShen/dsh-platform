"""页面数据源（P4-T05 / M4a-W44 的只读部分）。

每个数据源一个函数，入参只能来自 Schema 的 `params` 与 token 里的 tenant/user，
不接受任意 SQL / URL。跨租户统一按"不存在"处理（404），不泄露对象是否存在。
"""

from __future__ import annotations

from typing import Any

from ao.db import fetch_all, fetch_one

MAX_ROWS = 200

#: 允许出现在响应里的 run 字段（不含 error 堆栈、lease_owner、native_id 等内部信息）
_RUN_FIELDS = (
    "id",
    "workflow_id",
    "version",
    "status",
    "trigger_user_id",
    "created_at",
    "started_at",
    "ended_at",
    "spent_usd",
)


class DataSourceError(RuntimeError):
    """数据源非法或对象越权；调用方统一映射 404/400。"""


def run_list(*, tenant_id: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    """`run.list`：本租户的 run 列表，按创建时间倒序，最多 200 行。

    params 只认 `workflow_id` / `status`；其他键忽略而不是拼进 SQL。
    """
    clauses = ["tenant_id = %s"]
    values: list[Any] = [tenant_id]
    workflow_id = params.get("workflow_id")
    if isinstance(workflow_id, str) and workflow_id:
        clauses.append("workflow_id = %s")
        values.append(workflow_id)
    status = params.get("status")
    if isinstance(status, str) and status:
        clauses.append("status = %s")
        values.append(status)
    limit = _limit(params.get("limit"))
    values.append(limit)
    rows = fetch_all(
        f"SELECT {', '.join(_RUN_FIELDS)} FROM runs WHERE {' AND '.join(clauses)}"
        " ORDER BY created_at DESC LIMIT %s",
        tuple(values),
    )
    return [dict(row) for row in rows]


def run_get(*, tenant_id: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    """`run.get`：单条 run；跨租户返回空数组（端点层再翻成 404）。"""
    run_id = params.get("id")
    if not isinstance(run_id, str) or not run_id:
        raise DataSourceError("run.get 需要 id 参数")
    row = fetch_one(
        f"SELECT {', '.join(_RUN_FIELDS)} FROM runs WHERE id = %s AND tenant_id = %s",
        (run_id, tenant_id),
    )
    return [dict(row)] if row else []


def run_events(*, tenant_id: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    """`run.events`：单条 run 的事件流；同样按租户过滤，事件字段白名单输出。"""
    run_id = params.get("id")
    if not isinstance(run_id, str) or not run_id:
        raise DataSourceError("run.events 需要 id 参数")
    exists = fetch_one(
        "SELECT 1 AS ok FROM runs WHERE id = %s AND tenant_id = %s", (run_id, tenant_id)
    )
    if exists is None:
        return []
    rows = fetch_all(
        "SELECT seq, type, node_id, payload, created_at FROM run_events"
        " WHERE run_id = %s ORDER BY seq DESC LIMIT %s",
        (run_id, _limit(params.get("limit"))),
    )
    return [dict(row) for row in rows]


def dispatch(source: str, *, tenant_id: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    """按 Schema 声明的 `source` 分发；未实现的数据源明确报错而不是返回空。"""
    if source == "run.list":
        return run_list(tenant_id=tenant_id, params=params)
    if source == "run.get":
        return run_get(tenant_id=tenant_id, params=params)
    if source == "run.events":
        return run_events(tenant_id=tenant_id, params=params)
    if source in {"artifact.read", "mcp.read"}:
        raise DataSourceError(f"{source} 尚未实现（M4a-W44 待补）")
    raise DataSourceError(f"未声明的数据源: {source}")


def _limit(raw: Any) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 50
    return max(1, min(MAX_ROWS, value))
