"""运维写操作留痕（P3-T08 / M2-W22）。

与 `run_events` 的分工：
- `run_events` 回答"run 内部发生了什么"（节点开始/结束、重试、预算暂停）；
- `operator_actions` 回答"谁在什么时候对哪个对象按了什么按钮、成没成"。

只 append：没有更新入口，也没有删除入口。越权尝试必须落 `result='denied'`，
否则无法回答"有人试过但被挡了"。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from psycopg.types.json import Jsonb

from ao.db import fetch_all, tx
from ao.util import new_id


def record(
    *,
    action: str,
    object_type: str,
    object_id: str | None = None,
    actor: str = "anonymous",
    scope: str | None = None,
    scope_id: str | None = None,
    result: str = "ok",
    detail: str = "",
    payload: dict[str, Any] | None = None,
) -> None:
    """写一条运维审计。失败不应该影响主操作，所以调用方一般包一层 try。

    `result` 只允许 ok/denied/failed（与迁移 013 的 CHECK 一致）；写错值直接报错，
    避免出现"审计记了但状态不可读"。
    """
    if result not in {"ok", "denied", "failed"}:
        raise ValueError(f"result 只支持 ok/denied/failed: {result!r}")
    with tx() as conn:
        conn.execute(
            "INSERT INTO operator_actions (id, actor, action, object_type, object_id,"
            " scope, scope_id, result, detail, payload)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                new_id("oact"),
                actor,
                action,
                object_type,
                object_id,
                scope,
                scope_id,
                result,
                detail[:500],
                Jsonb(payload or {}),
            ),
        )


def query(
    *,
    actor: str | None = None,
    object_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """按 actor / 对象 / 时间窗查审计，新的在前。limit 上限 1000 防拖库。"""
    clauses: list[str] = []
    params: list[Any] = []
    if actor:
        clauses.append("actor = %s")
        params.append(actor)
    if object_id:
        clauses.append("object_id = %s")
        params.append(object_id)
    if since:
        clauses.append("created_at >= %s")
        params.append(since)
    if until:
        clauses.append("created_at <= %s")
        params.append(until)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, min(1000, limit)))
    return [
        dict(row)
        for row in fetch_all(
            "SELECT id, actor, action, object_type, object_id, scope, scope_id,"
            f" result, detail, payload, created_at FROM operator_actions{where}"
            " ORDER BY created_at DESC LIMIT %s",
            tuple(params),
        )
    ]
