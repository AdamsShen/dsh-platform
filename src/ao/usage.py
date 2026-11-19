"""记账。

真实模型走 LiteLLM 网关，账单以网关 spend log 为准；这里是**镜像**：
一次模型调用一行，`runs.spent_usd` 由聚合刷新，不做增量累加（避免接管重跑后账目翻倍）。
mock 模式下没有网关，按价目表估算，并在 `llm_usage.model` 里标 `mock-1`。
"""

from __future__ import annotations

import os
from typing import Any

import psycopg

from ao.db import fetch_all

PRICE_IN = float(os.environ.get("AO_PRICE_IN", "0.00027"))
PRICE_OUT = float(os.environ.get("AO_PRICE_OUT", "0.0011"))


def estimate_cost(input_tokens: int, output_tokens: int) -> float:
    return input_tokens / 1000.0 * PRICE_IN + output_tokens / 1000.0 * PRICE_OUT


def record(
    conn: psycopg.Connection,
    *,
    run_id: str | None,
    node_id: str | None,
    attempt: int,
    usage: dict[str, Any],
    purpose: str = "node",
    gateway_request_id: str | None = None,
    user_id: str | None = None,
) -> float:
    """记一次模型调用；创作会话用 `run_id=None` + `purpose='authoring'`（不归属任何 run）。

    `user_id` 是业务主体（扇出时每个 run 的 `trigger_user_id`），冗余一列是为了
    "按用户查费用"不用每次 JOIN `runs.subject->>'user_id'` 扫 JSONB。
    """
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    cached_tokens = int(usage.get("cached_tokens") or 0)
    cost = usage.get("cost")
    cost = float(cost) if cost is not None else estimate_cost(input_tokens, output_tokens)
    conn.execute(
        "INSERT INTO llm_usage (run_id, node_id, attempt, purpose, model, input_tokens,"
        " cached_tokens, output_tokens, cost, gateway_request_id, user_id)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            run_id,
            node_id,
            attempt,
            purpose,
            str(usage.get("model") or "unknown"),
            input_tokens,
            cached_tokens,
            output_tokens,
            cost,
            gateway_request_id,
            user_id,
        ),
    )
    return cost


def summary_by_user(
    *, tenant_id: str | None = None, since: Any = None, limit: int = 100
) -> list[dict[str, Any]]:
    """按用户汇总费用，高的在前。

    口径：只统计 `llm_usage.user_id` 非空的行（创作会话不归属用户）；
    合计应等于同条件下的 `SUM(cost)`——这是"三处账目一致"里最容易被破坏的一环，
    有差异就是缺陷，不做"误差容忍"。
    """
    clauses = ["u.user_id IS NOT NULL"]
    params: list[Any] = []
    if tenant_id:
        clauses.append("r.tenant_id = %s")
        params.append(tenant_id)
    if since:
        clauses.append("u.created_at >= %s")
        params.append(since)
    params.append(max(1, min(1000, limit)))
    rows = fetch_all(
        "SELECT u.user_id, COUNT(*) AS calls, SUM(u.cost) AS cost,"
        " SUM(u.input_tokens) AS input_tokens, SUM(u.output_tokens) AS output_tokens"
        " FROM llm_usage u LEFT JOIN runs r ON r.id = u.run_id"
        f" WHERE {' AND '.join(clauses)}"
        " GROUP BY u.user_id ORDER BY cost DESC LIMIT %s",
        tuple(params),
    )
    return [dict(row) for row in rows]


def refresh_run_spend(conn: psycopg.Connection, run_id: str) -> float:
    row = conn.execute(
        "UPDATE runs SET spent_usd = COALESCE("
        "  (SELECT SUM(cost) FROM llm_usage WHERE run_id = %s), 0)"
        " WHERE id = %s RETURNING spent_usd",
        (run_id, run_id),
    ).fetchone()
    return float(row["spent_usd"]) if row else 0.0
