"""run 状态机与失败分类（设计 §9.1 / §9.4）。

所有转移都用 `WHERE status = ANY(期望状态)` 做乐观校验：并发下只有一个能成功。
402 永远不能被当成瞬态失败重试，否则预算耗尽后空转烧钱。
"""

from __future__ import annotations

from typing import Any

from ao import events
from ao.db import tx

TRANSIENT = {"timeout", "provider_error", "internal_error"}
RETRYABLE_ONCE = {"missing_var"}
NO_RETRY = {"contract_error", "sandbox_denied", "template_error", "fenced"}


def transition(
    run_id: str,
    *,
    expect: tuple[str, ...],
    to: str,
    error: str | None = None,
    actor: str = "system",
    payload: dict[str, Any] | None = None,
) -> bool:
    with tx() as conn:
        row = conn.execute(
            "SELECT status FROM runs WHERE id = %s", (run_id,)
        ).fetchone()
        if row is None or row["status"] not in expect:
            return False
        conn.execute(
            "UPDATE runs SET status = %s, error = %s,"
            " ended_at = CASE WHEN %s IN ('succeeded','failed','cancelled')"
            " THEN now() ELSE ended_at END"
            " WHERE id = %s",
            (to, error, to, run_id),
        )
        events.emit(
            conn,
            run_id,
            "run.status_changed",
            payload={"from": row["status"], "to": to, "error": error, **(payload or {})},
            actor=actor,
        )
    return True


def resume(run_id: str, actor: str = "system") -> bool:
    """放行 paused_budget / waiting_hitl：回队列，epoch + 1（旧 owner 写不进来）。"""
    with tx() as conn:
        updated = conn.execute(
            "UPDATE runs SET status='queued', epoch = epoch + 1, retry_at = NULL,"
            " lease_owner = NULL, lease_until = NULL, error = NULL, cancel_requested = false"
            " WHERE id = %s AND status IN ('paused_budget','waiting_hitl')",
            (run_id,),
        )
        if updated.rowcount:
            events.emit(conn, run_id, "run.resumed", actor=actor)
        return updated.rowcount > 0


def requeue(run_id: str, actor: str = "system") -> bool:
    """人工重试：把失败/已取消的 run 放回队列，epoch + 1。

    只接受 `failed` / `cancelled`：`succeeded` 重跑会重复产生外部副作用，
    需要的是新建 run 而不是复用同一个 id（复用会让审计与计费串在一起）。
    节点与 checkpoint 保持原样，执行器从已完成节点之后继续。
    """
    with tx() as conn:
        updated = conn.execute(
            "UPDATE runs SET status='queued', epoch = epoch + 1, retry_at = NULL,"
            " lease_owner = NULL, lease_until = NULL, error = NULL, ended_at = NULL,"
            " cancel_requested = false"
            " WHERE id = %s AND status IN ('failed','cancelled')",
            (run_id,),
        )
        if updated.rowcount:
            events.emit(conn, run_id, "run.requeued", actor=actor)
        return updated.rowcount > 0


def request_cancel(run_id: str, actor: str = "system") -> str:
    """停止只在节点边界生效；queued 的直接取消，running 的置标志等 worker 收尾。"""
    with tx() as conn:
        row = conn.execute(
            "SELECT status FROM runs WHERE id = %s FOR UPDATE", (run_id,)
        ).fetchone()
        if row is None:
            return "missing"
        if row["status"] == "queued":
            conn.execute(
                "UPDATE runs SET status = 'cancelled', cancel_requested = true,"
                " ended_at = now() WHERE id = %s",
                (run_id,),
            )
            events.emit(conn, run_id, "run.cancelled", payload={"immediate": True}, actor=actor)
            return "cancelled"
        if row["status"] in {"succeeded", "failed", "cancelled"}:
            return row["status"]
        conn.execute("UPDATE runs SET cancel_requested = true WHERE id = %s", (run_id,))
        events.emit(conn, run_id, "run.cancel_requested", actor=actor)
        return "cancelling"


def schedule_retry(run_id: str, *, code: str, error: str, backoff: float = 30.0) -> str:
    """按失败分类决定结局；返回最终状态。"""
    if code == "budget_exceeded":
        # 402 是"可放行的可恢复态"，不是失败，更不能走瞬态重试
        with tx() as conn:
            conn.execute(
                "UPDATE runs SET status='paused_budget', error=%s, lease_owner=NULL,"
                " lease_until=NULL, ended_at=NULL"
                " WHERE id=%s AND status NOT IN ('succeeded','failed','cancelled')",
                (f"{code}: {error}", run_id),
            )
            events.emit(
                conn, run_id, "run.paused_budget", payload={"code": code, "error": error}
            )
        return "paused_budget"
    if code in TRANSIENT:
        with tx() as conn:
            row = conn.execute(
                "SELECT retry_count, workflow_id, version, subject, package_ref, tenant_id,"
                " dedupe_key, trigger_user_id, budget_usd FROM runs WHERE id = %s FOR UPDATE",
                (run_id,),
            ).fetchone()
            if row is None:
                return "missing"
            if int(row["retry_count"]) >= 3:
                conn.execute(
                    "UPDATE runs SET status='failed', error=%s, ended_at=now() WHERE id=%s",
                    (f"{code}: {error}", run_id),
                )
                events.emit(conn, run_id, "run.retry_exhausted", payload={"code": code})
                return "failed"
            conn.execute(
                "UPDATE runs SET status='queued', retry_count = retry_count + 1,"
                " retry_at = now() + make_interval(secs => %s), error = %s,"
                " lease_owner = NULL, lease_until = NULL, epoch = epoch + 1"
                " WHERE id = %s",
                (backoff, f"{code}: {error}", run_id),
            )
            events.emit(
                conn,
                run_id,
                "run.retry_scheduled",
                payload={"code": code, "backoff": backoff, "retry_count": row["retry_count"] + 1},
            )
        return "queued"
    if code in RETRYABLE_ONCE:
        with tx() as conn:
            row = conn.execute(
                "SELECT retry_count FROM runs WHERE id = %s FOR UPDATE", (run_id,)
            ).fetchone()
            if row is not None and int(row["retry_count"]) < 1:
                conn.execute(
                    "UPDATE runs SET status='queued', retry_count = retry_count + 1,"
                    " retry_at = now(), error = %s, lease_owner = NULL, lease_until = NULL,"
                    " epoch = epoch + 1 WHERE id = %s",
                    (f"{code}: {error}", run_id),
                )
                events.emit(conn, run_id, "run.retry_scheduled", payload={"code": code})
                return "queued"
    with tx() as conn:
        conn.execute(
            "UPDATE runs SET status='failed', error=%s, ended_at=now() WHERE id=%s",
            (f"{code}: {error}", run_id),
        )
        events.emit(conn, run_id, "run.failed", payload={"code": code, "error": error})
    return "failed"
