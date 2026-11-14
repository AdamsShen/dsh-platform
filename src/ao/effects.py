"""外部副作用的幂等日志（effect journal）。

为什么需要：LangGraph 的恢复粒度是节点边界，节点内崩溃后整个节点重跑
（见 `docs/research/2026-11-13-execution-kernel.md` §1），`action()` 会被再调一次。
框架不保证 exactly-once，重复抑制只能在动作边界自己做。

键是 `(run_id, node_id, action, params_hash)`：

- **不含 attempt**：跨 attempt 复用才叫幂等；参数变了 hash 就变，自然是另一个副作用。
- **同名同参视为同一副作用**：循环里要发多条消息，请在参数里带区分字段（如 `index`）。

接管语义与队列一致，用 `owner_epoch` 而不是时钟：

- 同一 epoch 内重复认领且未超过 stale → `EffectInFlight`（防同一进程并发双写）；
- 行的 `owner_epoch` 更小 → 上一任已失权，直接 CAS 抢占，不必等 stale；
- 同一 epoch 且超过 stale → 视为卡死，CAS 抢占。
"""

from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from psycopg.types.json import Jsonb

from ao.db import tx

#: 同一 owner 多久没更新视为卡死；必须大于节点最长执行时间（默认 1800s）
STALE_SECONDS = 1800.0


class EffectInFlight(RuntimeError):
    """同名同参的副作用正在被另一个执行者跑；此时不能并发再跑一次。"""

    def __init__(self, action: str, node_id: str) -> None:
        super().__init__(f"副作用正在执行中: node={node_id} action={action}")
        self.action = action
        self.node_id = node_id


def params_hash(params: Mapping[str, Any]) -> str:
    """稳定 hash：键序无关、非 JSON 类型按 `str` 降级，避免同一参数算出两个键。"""
    payload = json.dumps(
        dict(params),
        sort_keys=True,
        ensure_ascii=False,
        default=str,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


@dataclass
class _Claim:
    replay: bool
    result: Any = None


def _jsonable(value: Any) -> Any:
    """结果要落 JSONB；不可序列化的部分按 `str` 存，但返回给调用方的仍是原值。"""
    if value is None:
        return None
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _claim(
    *,
    run_id: str,
    node_id: str,
    action: str,
    digest: str,
    epoch: int,
    stale_seconds: float,
) -> _Claim:
    key = (run_id, node_id, action, digest)
    with tx() as conn:
        inserted = conn.execute(
            "INSERT INTO run_effects (run_id, node_id, action, params_hash, owner_epoch, attempt)"
            " VALUES (%s,%s,%s,%s,%s,0)"
            " ON CONFLICT DO NOTHING"
            " RETURNING status",
            (*key, epoch),
        ).fetchone()
        if inserted is not None:
            return _Claim(replay=False)

        row = conn.execute(
            "SELECT status, owner_epoch, result, updated_at,"
            " (now() - updated_at) < make_interval(secs => %s) AS fresh"
            " FROM run_effects"
            " WHERE run_id=%s AND node_id=%s AND action=%s AND params_hash=%s"
            " FOR UPDATE",
            (stale_seconds, *key),
        ).fetchone()
        if row is None:  # 并发删除，不可能是正常路径
            raise EffectInFlight(action, node_id)

        if row["status"] == "succeeded":
            # 重放：返回上次的结果，不再真执行
            return _Claim(replay=True, result=row["result"])

        if row["status"] == "started" and int(row["owner_epoch"]) >= epoch and row["fresh"]:
            raise EffectInFlight(action, node_id)

        updated = conn.execute(
            "UPDATE run_effects"
            " SET status='started', owner_epoch=%s, attempt=attempt+1, error=NULL, updated_at=now()"
            " WHERE run_id=%s AND node_id=%s AND action=%s AND params_hash=%s"
            " AND updated_at=%s",
            (epoch, *key, row["updated_at"]),
        )
        if updated.rowcount == 0:
            # 被别人先抢到了（CAS 失败），交给对方执行
            raise EffectInFlight(action, node_id)
        return _Claim(replay=False)


def _settle(
    *,
    run_id: str,
    node_id: str,
    action: str,
    digest: str,
    epoch: int,
    status: str,
    result: Any = None,
    error: str | None = None,
) -> None:
    with tx() as conn:
        conn.execute(
            "UPDATE run_effects SET status=%s, result=%s, error=%s, updated_at=now()"
            " WHERE run_id=%s AND node_id=%s AND action=%s AND params_hash=%s"
            " AND owner_epoch=%s",
            (status, Jsonb(_jsonable(result)) if result is not None else None, error,
             run_id, node_id, action, digest, epoch),
        )


async def run_once(
    *,
    run_id: str,
    node_id: str,
    action: str,
    params: Mapping[str, Any],
    epoch: int,
    fn: Callable[[], Any] | Callable[[], Awaitable[Any]],
    stale_seconds: float = STALE_SECONDS,
) -> Any:
    """执行一次副作用；同键已成功时直接返回上次结果。

    `fn` 可以是同步或异步函数。执行失败会把行标成 `failed` 并原样抛出，
    上层重试同一节点时会重新执行（失败不是终态，除非 run 本身失败）。
    """
    digest = params_hash(params)
    claim = _claim(
        run_id=run_id,
        node_id=node_id,
        action=action,
        digest=digest,
        epoch=epoch,
        stale_seconds=stale_seconds,
    )
    if claim.replay:
        return claim.result

    try:
        value = fn()
        if inspect.isawaitable(value):
            value = await value
    except BaseException as exc:  # noqa: BLE001 — 失败要落痕再抛，不能吞
        _settle(
            run_id=run_id,
            node_id=node_id,
            action=action,
            digest=digest,
            epoch=epoch,
            status="failed",
            error=f"{type(exc).__name__}: {exc}"[:500],
        )
        raise

    _settle(
        run_id=run_id,
        node_id=node_id,
        action=action,
        digest=digest,
        epoch=epoch,
        status="succeeded",
        result=value,
    )
    return value
