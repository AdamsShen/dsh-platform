"""PG 租约队列：`FOR UPDATE SKIP LOCKED` + 30s 租约 + 10s 心跳。

不在内存里排队：入口 burst 先落 `runs(status='queued')`，worker 来领。
租约过期只代表"可能死了"，回收时 `epoch + 1`，旧进程之后所有写都会被 fencing 拒绝。
"""

from __future__ import annotations

from typing import Any

from ao import events
from ao.db import fetch_all, fetch_one, tx

LEASE_SECONDS = 30
HEARTBEAT_SECONDS = 10


def claim_next(worker_id: str) -> dict[str, Any] | None:
    with tx() as conn:
        row = conn.execute(
            "WITH candidate AS ("
            "  SELECT id FROM runs"
            "  WHERE status = 'queued'"
            "    AND (retry_at IS NULL OR retry_at <= now())"
            "    AND cancel_requested = false"
            "  ORDER BY created_at"
            "  FOR UPDATE SKIP LOCKED"
            "  LIMIT 1"
            ")"
            " UPDATE runs SET status = 'running',"
            "   started_at = COALESCE(started_at, now()),"
            "   lease_owner = %s,"
            "   lease_until = now() + make_interval(secs => %s)"
            " FROM candidate WHERE runs.id = candidate.id"
            " RETURNING runs.*",
            (worker_id, LEASE_SECONDS),
        ).fetchone()
        if row:
            events.emit(
                conn,
                str(row["id"]),
                "run.claimed",
                payload={"worker": worker_id, "epoch": row["epoch"]},
            )
        return row


def renew_lease(run_id: str, worker_id: str, epoch: int) -> bool:
    """续租；返回 False 表示已不是 owner（交给 fencing 处理）。"""
    with tx() as conn:
        updated = conn.execute(
            "UPDATE runs SET lease_until = now() + make_interval(secs => %s)"
            " WHERE id = %s AND lease_owner = %s AND epoch = %s AND status = 'running'",
            (LEASE_SECONDS, run_id, worker_id, epoch),
        )
        return updated.rowcount > 0


def heartbeat(worker_id: str, *, host: str, pool: str = "execution", slots: int = 1) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT INTO worker_heartbeats (worker_id, host, pool, slots, last_seen)"
            " VALUES (%s,%s,%s,%s,now())"
            " ON CONFLICT (worker_id) DO UPDATE SET last_seen = now(), slots = EXCLUDED.slots",
            (worker_id, host, pool, slots),
        )
        conn.execute(
            "UPDATE runs SET lease_until = now() + make_interval(secs => %s)"
            " WHERE lease_owner = %s AND status = 'running'",
            (LEASE_SECONDS, worker_id),
        )


def reclaim_expired() -> list[dict[str, Any]]:
    """回收过期租约：回 queued 并 epoch + 1。由 scheduler 周期调用。"""
    with tx() as conn:
        rows = conn.execute(
            "UPDATE runs SET status = 'queued', lease_owner = NULL, lease_until = NULL,"
            " epoch = epoch + 1"
            " WHERE status = 'running' AND lease_until IS NOT NULL AND lease_until < now()"
            " RETURNING id, epoch, workflow_id"
        ).fetchall()
        for row in rows:
            events.emit(
                conn,
                str(row["id"]),
                "run.taken_over",
                payload={"reason": "lease_expired", "new_epoch": row["epoch"]},
            )
    return rows


def dead_workers(ttl_seconds: int = 30) -> list[dict[str, Any]]:
    return fetch_all(
        "SELECT * FROM worker_heartbeats WHERE last_seen < now() - make_interval(secs => %s)",
        (ttl_seconds,),
    )


def pending_count(tenant_id: str | None = None) -> int:
    if tenant_id:
        row = fetch_one(
            "SELECT COUNT(*) AS n FROM runs"
            " WHERE tenant_id = %s AND status IN ('queued','running')",
            (tenant_id,),
        )
    else:
        row = fetch_one("SELECT COUNT(*) AS n FROM runs WHERE status IN ('queued','running')")
    return int(row["n"]) if row else 0
