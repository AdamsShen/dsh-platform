"""事件表：所有状态转移与工具调用都落这里，审计与详情页都读它。"""

from __future__ import annotations

from typing import Any

import psycopg

from ao.util import to_json


def emit(
    conn: psycopg.Connection,
    run_id: str,
    type_: str,  # noqa: A002 — 与设计文档的事件名保持同名参数
    *,
    node_id: str | None = None,
    payload: dict[str, Any] | None = None,
    actor: str = "system",
) -> None:
    conn.execute(
        "INSERT INTO run_events (run_id, node_id, type, payload, actor)"
        " VALUES (%s, %s, %s, %s::jsonb, %s)",
        (run_id, node_id, type_, to_json(payload or {}), actor),
    )


def list_for_run(run_id: str, *, limit: int = 500, after_seq: int = 0) -> list[dict[str, Any]]:
    from ao.db import read

    with read() as conn:
        return conn.execute(
            "SELECT * FROM run_events WHERE run_id = %s AND seq > %s ORDER BY seq LIMIT %s",
            (run_id, after_seq, limit),
        ).fetchall()
