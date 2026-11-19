"""页面数据源（P4-T05 / M4a-W44 的只读部分）。

每个数据源一个函数，入参只能来自 Schema 的 `params` 与 token 里的 tenant/user，
不接受任意 SQL / URL。跨租户统一按"不存在"处理（404），不泄露对象是否存在。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ao import mcp, share
from ao.db import fetch_all, fetch_one

MAX_ROWS = 200
#: 单份产物最多返回的字符数：看板不是文件下载器，超长内容该走 artifact 详情接口
ARTIFACT_MAX_CHARS = 64_000

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


def run_list(
    *, tenant_id: str, params: dict[str, Any], scope: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """`run.list`：本租户的 run 列表，按创建时间倒序，最多 200 行。

    params 只认 `workflow_id` / `status`；其他键忽略而不是拼进 SQL。
    `scope` 来自分享链接：编译成额外谓词，先于取数生效（不是取回再过滤）。
    """
    clauses = ["tenant_id = %s"]
    values: list[Any] = [tenant_id]
    if scope is not None:
        scope_clause, scope_values = share.scope_sql(
            scope, default_workflow_id=str(params.get("workflow_id") or "")
        )
        clauses.append(scope_clause)
        values.extend(scope_values)
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


def artifact_read(*, tenant_id: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    """`artifact.read`：读 run 所属包内 `artifacts/<name>` 的文本/JSON 产物。

    只读包内文件，不读工作区（工作区是执行中间态，可能含未脱敏输入）。
    """
    run_id = params.get("id")
    name = params.get("name")
    if not isinstance(run_id, str) or not run_id:
        raise DataSourceError("artifact.read 需要 id 参数")
    if not isinstance(name, str) or not name:
        raise DataSourceError("artifact.read 需要 name 参数")
    row = fetch_one(
        "SELECT package_ref FROM runs WHERE id = %s AND tenant_id = %s", (run_id, tenant_id)
    )
    if row is None:
        return []
    base = (Path(str(row["package_ref"])) / "artifacts").resolve()
    target = (base / name).resolve()
    if base not in target.parents or not target.is_file():
        return []
    text = target.read_text(encoding="utf-8", errors="replace")[:ARTIFACT_MAX_CHARS]
    try:
        return [{"name": name, "json": json.loads(text)}]
    except json.JSONDecodeError:
        return [{"name": name, "text": text}]


def mcp_read(*, tenant_id: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    """`mcp.read`：只允许调只读工具；写工具在 `mcp.call_readonly` 里被挡。"""
    server = params.get("server")
    tool = params.get("tool")
    if not isinstance(server, str) or not server:
        raise DataSourceError("mcp.read 需要 server 参数")
    if not isinstance(tool, str) or not tool:
        raise DataSourceError("mcp.read 需要 tool 参数")
    args = {k: v for k, v in params.items() if k not in {"server", "tool", "limit"}}
    try:
        result = mcp.call_readonly(server, tool, args)
    except mcp.MCPError as exc:
        raise DataSourceError(str(exc)) from exc
    if isinstance(result, dict):
        return [dict(result)]
    return [{"value": result}]


def dispatch(
    source: str,
    *,
    tenant_id: str,
    params: dict[str, Any],
    scope: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """按 Schema 声明的 `source` 分发；未声明的数据源明确报错而不是返回空。"""
    if source == "run.list":
        return run_list(tenant_id=tenant_id, params=params, scope=scope)
    if source == "run.get":
        return run_get(tenant_id=tenant_id, params=params)
    if source == "run.events":
        return run_events(tenant_id=tenant_id, params=params)
    if source == "artifact.read":
        return artifact_read(tenant_id=tenant_id, params=params)
    if source == "mcp.read":
        return mcp_read(tenant_id=tenant_id, params=params)
    raise DataSourceError(f"未声明的数据源: {source}")


def _limit(raw: Any) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 50
    return max(1, min(MAX_ROWS, value))
