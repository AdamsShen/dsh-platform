"""定时/事件触发器：发布投影 + 到点扫描 + 事件入站 + 重叠策略 + 手动回填。

设计要点（对照 Temporal Schedules、Quartz misfire、K8s CronJob）：

- 计划是独立对象（`triggers` 表），与版本解耦：改计划只需重新投影，不用重跑流程；
- 抢占用 CAS（`UPDATE ... WHERE next_fire_at = 旧值`），叠加 scheduler 的 advisory lock，
  保证同一时刻只被处理一次；状态全在表里，进程重启后照样续跑；
- 重叠策略可配（P2-T19，对照 Temporal ScheduleOverlapPolicy）：
  `skip` 跳过 / `buffer_one` 缓冲一个 / `cancel_other` 置旧 run 取消标志后启动新的。
  不提供 terminate_other——本项目停止只在节点边界生效（DSH 单轮无 cancel）；
- 缓冲落库（`pending_fire_at`）而不是内存队列：调度器无状态、可多副本，重启不丢；
- 错过时刻按**窗口 + 次数上限**回填（Quartz misfireThreshold / K8s 100 次上限的启发）：
  窗口外的老时刻直接丢弃并留审计，避免停机一天后一次补几十次；
- 手动 backfill 是独立入口，按区间枚举槽位并忽略重叠检查（对齐 Temporal 建议的
  AllowAll/BufferAll 回填姿势），不改 `next_fire_at`，只补历史；
- 事件触发用幂等键去重，且不做重叠跳过——事件是显式意图，排队执行即可。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg.types.json import Jsonb

from ao import runs as runs_store
from ao import state as run_state
from ao import users as users_store
from ao.cron import next_fire, validate_cron
from ao.db import fetch_all, fetch_one, tx
from ao.settings import settings
from ao.util import new_id, sha256_hex

logger = logging.getLogger(__name__)

#: 回填时最多向前推进多少次；正常窗口远到不了，纯属防死循环的保险
MAX_SKIP_STEPS = 2000

#: 回填参数上限：窗口最多 30 天、一次最多补 20 次
MAX_CATCHUP_WINDOW = 30 * 24 * 3600
MAX_CATCHUP_LIMIT = 20

#: 单次扇出上限（与迁移 012 的 CHECK 约束保持一致）
MAX_FANOUT_LIMIT = 5000

#: 支持的重叠策略（与迁移 009 的 CHECK 约束保持一致）
OVERLAP_POLICIES = ("skip", "buffer_one", "cancel_other")

#: 手动 backfill：区间最多 31 天、一次最多 200 个槽位
MAX_BACKFILL_SPAN = 31 * 24 * 3600
MAX_BACKFILL_SLOTS = 200
DEFAULT_BACKFILL_LIMIT = 50

#: 缓冲槽位最长等待时间：run 卡死时避免 pending 永久挂住
PENDING_TTL_SECONDS = 24 * 3600


def project(
    *,
    workflow_id: str,
    version: int,
    tenant_id: str,
    owner_user_id: str | None,
    package_ref: str,
    triggers: list[Any],
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """发布投影：同一工作流的旧计划全部失效，再写入当前版本的计划。

    `triggers` 是 manifest 里的 Trigger 对象（或 dict）；manual 类型不投影。
    """
    now = now or datetime.now(UTC)
    planned: list[dict[str, Any]] = []
    for trigger in triggers or []:
        kind = str(_field(trigger, "type") or "")
        fanout = _normalize_fanout(_field(trigger, "fanout"))
        if kind == "cron":
            cron = str(_field(trigger, "cron") or "").strip()
            if not cron:
                continue
            timezone = str(_field(trigger, "timezone") or "Asia/Shanghai")
            validate_cron(cron, timezone)  # 发布期已校验，这里是兜底
            planned.append(
                {
                    "type": "cron",
                    "cron": cron,
                    "timezone": timezone,
                    "event_name": None,
                    "next": next_fire(cron, timezone, now),
                    "window": _bounded(
                        _field(trigger, "catchup_window_seconds"),
                        3600,
                        low=0,
                        high=MAX_CATCHUP_WINDOW,
                    ),
                    "limit": _bounded(
                        _field(trigger, "catchup_limit"),
                        1,
                        low=1,
                        high=MAX_CATCHUP_LIMIT,
                    ),
                    # 发布期已校验，这里再兜底一次：脏值一律回落 skip（不静默跳过重叠保护）
                    "overlap_policy": _policy(_field(trigger, "overlap_policy")),
                    "fanout_mode": fanout["mode"],
                    "fanout_spec": fanout["spec"],
                    "fanout_limit": fanout["limit"],
                    "budget_per_run": fanout["budget"],
                }
            )
        elif kind == "event":
            event_name = str(_field(trigger, "event_name") or "").strip()
            if not event_name:
                continue
            # 事件型没有"下次时刻"，回填参数也无意义
            planned.append(
                {
                    "type": "event",
                    "cron": None,
                    "timezone": "Asia/Shanghai",
                    "event_name": event_name,
                    "next": None,
                    "window": 0,
                    "limit": 1,
                    "overlap_policy": "skip",
                    "fanout_mode": fanout["mode"],
                    "fanout_spec": fanout["spec"],
                    "fanout_limit": fanout["limit"],
                    "budget_per_run": fanout["budget"],
                }
            )

    rows: list[dict[str, Any]] = []
    with tx() as conn:
        # 先把旧计划关掉，避免唯一索引 (workflow_id) WHERE enabled 冲突
        conn.execute(
            "UPDATE triggers SET enabled = false WHERE workflow_id = %s AND enabled",
            (workflow_id,),
        )
        for item in planned:
            row = conn.execute(
                "INSERT INTO triggers (id, workflow_id, version, tenant_id, owner_user_id,"
                " type, cron, timezone, event_name, package_ref, enabled, next_fire_at,"
                " catchup_window_seconds, catchup_limit, overlap_policy,"
                " fanout_mode, fanout_spec, fanout_limit, budget_per_run)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,true,%s,%s,%s,%s,%s,%s,%s,%s)"
                " RETURNING *",
                (
                    new_id("trg"),
                    workflow_id,
                    version,
                    tenant_id,
                    owner_user_id,
                    item["type"],
                    item["cron"],
                    item["timezone"],
                    item["event_name"],
                    package_ref,
                    item["next"],
                    item["window"],
                    item["limit"],
                    item["overlap_policy"],
                    item["fanout_mode"],
                    Jsonb(item["fanout_spec"]),
                    item["fanout_limit"],
                    item["budget_per_run"],
                ),
            ).fetchone()
            rows.append(dict(row))
    if rows:
        logger.info(
            "触发计划已投影: workflow=%s version=%s 条数=%s 类型=%s",
            workflow_id,
            version,
            len(rows),
            [row["type"] for row in rows],
        )
    return rows


def for_workflow(workflow_id: str) -> list[dict[str, Any]]:
    """页面用：某个工作流的全部计划（含已失效的，按时间倒序）。"""
    return fetch_all(
        "SELECT * FROM triggers WHERE workflow_id = %s ORDER BY created_at DESC",
        (workflow_id,),
    )


def fire_due(*, now: datetime | None = None, limit: int = 20) -> list[dict[str, Any]]:
    """扫描到点的 cron 计划并创建 run；返回本轮的处理结果。

    先消费 `buffer_one` 缓冲：那些槽位在"当时有 run 在跑"时被挂起，现在
    上一次可能已经结束，必须在扫新槽位之前把它们拉起来。
    """
    now = now or datetime.now(UTC)
    results: list[dict[str, Any]] = _drain_pending(now=now, limit=limit)
    # 同一轮里刚拉起缓冲的计划不再处理新槽位：拉起后立刻又按重叠缓冲一次等于自激，
    # 且会让"一次扫描处理了几个槽位"的语义变得不可预测（`*/N` 定时器踩边界时尤其明显）
    just_drained = {str(item.get("trigger_id")) for item in results if item.get("trigger_id")}
    due = fetch_all(
        "SELECT * FROM triggers WHERE enabled AND type = 'cron' AND next_fire_at <= %s"
        " ORDER BY next_fire_at LIMIT %s",
        (now, limit),
    )
    for row in due:
        if str(row["id"]) in just_drained:
            continue
        try:
            results.extend(_fire(dict(row), now))
        except Exception:  # noqa: BLE001
            # 单个计划出错不能拖垮整轮扫描
            logger.exception(
                "定时触发异常: trigger=%s workflow=%s", row["id"], row["workflow_id"]
            )
    return results


def emit_event(
    *,
    workflow_id: str,
    event_name: str,
    payload: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """外部事件入站：匹配启用的 event 计划并创建 run。

    没有匹配计划时返回 ignored 而不是报错——webhook 对端收到 4xx 会重试，
    而"这个工作流没订阅该事件"不是可恢复错误。
    """
    now = now or datetime.now(UTC)
    row = fetch_one(
        "SELECT * FROM triggers WHERE workflow_id = %s AND type = 'event'"
        " AND event_name = %s AND enabled ORDER BY created_at DESC LIMIT 1",
        (workflow_id, event_name),
    )
    if row is None:
        return {"status": "ignored", "detail": f"没有启用的 {event_name} 事件计划"}

    key = (idempotency_key or "").strip() or new_id("evt")
    # 幂等只看外部事件键（+ 工作流与版本），payload 允许有细微差异：
    # 对端重试时常会带上新时间戳，若 payload 参与幂等就会重复触发
    dedupe = sha256_hex(f"event|{workflow_id}|{row['version']}|{key}")
    try:
        run_id, created = runs_store.create_run(
            tenant_id=row["tenant_id"],
            workflow_id=workflow_id,
            version=int(row["version"]),
            package_ref=row["package_ref"],
            subject={"event": event_name, "payload": payload or {}},
            window=f"event:{key}",
            trigger_user_id=row["owner_user_id"],
            dedupe_key_override=dedupe,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("事件触发创建 run 失败: workflow=%s event=%s", workflow_id, event_name)
        _record(row, now, "failed", detail=str(exc)[:200], key=f"event:{key}")
        return {"status": "failed", "detail": str(exc)[:200]}

    status = "consumed" if created else "skipped"
    _record(row, now, status, detail=f"run {run_id}", run_id=run_id, key=f"event:{key}")
    logger.info(
        "事件触发: workflow=%s event=%s run=%s 新建=%s",
        workflow_id,
        event_name,
        run_id,
        created,
    )
    return {"status": status, "run_id": run_id, "created": created}


def _plan_slots(row: dict[str, Any], now: datetime) -> tuple[list[datetime], datetime]:
    """算出本轮该补的槽位，以及 CAS 要推进到的下一个时刻。

    返回 `(slots, upcoming)`：`slots` 按时间升序且都 `<= now`；窗口外的老时刻被跳过，
    所以才需要 `upcoming` 单独返回——即使一个槽位都不补，也要把指针推进到未来。
    """
    scheduled = row["next_fire_at"]
    window_seconds = int(row.get("catchup_window_seconds") or 0)
    limit = max(1, _bounded(row.get("catchup_limit"), 1, low=1, high=MAX_CATCHUP_LIMIT))
    floor = now - timedelta(seconds=window_seconds) if window_seconds > 0 else now

    cursor = scheduled
    steps = 0
    # 停机很久时先快速跳到窗口内，否则要逐条空转几百次
    while cursor < floor and steps < MAX_SKIP_STEPS:
        cursor = next_fire(row["cron"], row["timezone"], cursor)
        steps += 1

    slots: list[datetime] = []
    while cursor <= now and len(slots) < limit and steps < MAX_SKIP_STEPS:
        slots.append(cursor)
        cursor = next_fire(row["cron"], row["timezone"], cursor)
        steps += 1

    # 超过 limit 的错过时刻直接丢弃：指针一律推到 now 之后。否则下一轮还会接着补，
    # limit 就变成"每轮速率"而不是"总量上限"，停机一周能补出几百次。
    upcoming = cursor if cursor > now else next_fire(row["cron"], row["timezone"], now)
    return slots, upcoming


def _fire(row: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    scheduled = row["next_fire_at"]
    try:
        slots, upcoming = _plan_slots(row, now)
    except ValueError:
        logger.exception("cron 失效，禁用计划: trigger=%s cron=%s", row["id"], row["cron"])
        with tx() as conn:
            conn.execute("UPDATE triggers SET enabled = false WHERE id = %s", (row["id"],))
        _record(row, scheduled, "failed", detail="cron 失效")
        return [{"trigger_id": row["id"], "status": "failed", "detail": "cron 失效"}]

    # CAS 抢占：把下次触发时间推进到未来。影响 0 行说明别的调度器已经处理过这一时刻
    with tx() as conn:
        claimed = conn.execute(
            "UPDATE triggers SET next_fire_at = %s, last_fired_at = %s"
            " WHERE id = %s AND enabled AND next_fire_at = %s RETURNING id",
            (upcoming, now, row["id"], scheduled),
        ).fetchone()
    if claimed is None:
        return []

    if not slots:
        detail = f"错过时刻在回填窗口({row['catchup_window_seconds']}s)之外，整体跳过"
        _record(row, scheduled, "skipped", detail=detail)
        logger.info(
            "定时触发窗口外跳过: workflow=%s scheduled=%s now=%s",
            row["workflow_id"],
            scheduled.isoformat(),
            now.isoformat(),
        )
        return [{"trigger_id": row["id"], "status": "skipped", "detail": detail}]

    results: list[dict[str, Any]] = []
    for slot in slots:
        outcome = _dispatch(row, slot, now=now)
        results.append(outcome)
        if outcome["status"] != "consumed":
            # 已有一个 run 在跑，继续补只会全部 skip，直接结束本轮
            dropped = len(slots) - len(results)
            if dropped:
                _record(
                    row,
                    slots[-1],
                    "skipped",
                    detail=f"重叠中断，剩余 {dropped} 个错过时刻本轮放弃",
                    key=f"{row['id']}:dropped:{int(slots[-1].timestamp())}",
                )
            break
    return results


def _spawn(
    row: dict[str, Any],
    slot: datetime,
    *,
    policy: str | None = None,
    ignore_overlap: bool = False,
    audit_key: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """为一个计划时刻创建 run（含重叠策略分派与审计）。

    `policy` 默认取计划自身的 `overlap_policy`；`ignore_overlap=True` 只给 backfill 用——
    补历史时刻不该被"当前有 run 在跑"挡掉（对齐 Temporal 建议的回填姿势）。
    """
    now = now or datetime.now(UTC)
    policy = policy or _policy(row.get("overlap_policy"))
    running = None if ignore_overlap else _running_run(row["workflow_id"])

    if running:
        if policy == "skip":
            _record(
                row,
                slot,
                "skipped",
                detail=f"上一次还在跑: {running['id']}",
                key=audit_key,
            )
            logger.info(
                "定时触发跳过（重叠）: workflow=%s running_run=%s slot=%s",
                row["workflow_id"],
                running["id"],
                slot.isoformat(),
            )
            return {
                "trigger_id": row["id"],
                "status": "skipped",
                "policy": policy,
                "run_id": str(running["id"]),
                "scheduled_for": slot.isoformat(),
            }

        if policy == "buffer_one":
            # 缓冲只留一个：已有待跑槽位就丢弃新的（对齐 Temporal BUFFER_ONE）。
            # 想补最新时刻应走手动 backfill，而不是让缓冲无限覆盖。
            if row.get("pending_fire_at"):
                detail = f"缓冲已满: {row['pending_fire_at'].isoformat()}"
                _record(row, slot, "skipped", detail=detail, key=audit_key)
                return {
                    "trigger_id": row["id"],
                    "status": "skipped",
                    "policy": policy,
                    "detail": detail,
                    "scheduled_for": slot.isoformat(),
                }
            with tx() as conn:
                buffered = conn.execute(
                    "UPDATE triggers SET pending_fire_at = %s, pending_since = %s"
                    " WHERE id = %s AND pending_fire_at IS NULL",
                    (slot, now, row["id"]),
                ).rowcount
            if not buffered:
                # 并发下别的调度器刚写了缓冲，按"缓冲已满"处理
                _record(row, slot, "skipped", detail="缓冲已满", key=audit_key)
                return {
                    "trigger_id": row["id"],
                    "status": "skipped",
                    "policy": policy,
                    "scheduled_for": slot.isoformat(),
                }
            _record(
                row,
                slot,
                "buffered",
                detail=f"上一次还在跑: {running['id']}，槽位进缓冲",
                key=audit_key,
            )
            logger.info(
                "定时触发缓冲: workflow=%s run=%s slot=%s",
                row["workflow_id"],
                running["id"],
                slot.isoformat(),
            )
            return {
                "trigger_id": row["id"],
                "status": "buffered",
                "policy": policy,
                "run_id": str(running["id"]),
                "scheduled_for": slot.isoformat(),
            }

        # cancel_other：只请求取消（节点边界退出），不做强杀；随后照常建新 run，
        # 新 run 与正在收尾的旧 run 会短暂并存，这是 Temporal CancelOther 的既有语义
        outcome = run_state.request_cancel(str(running["id"]), actor="trigger:cancel_other")
        _record(
            row,
            slot,
            "cancelled",
            detail=f"被新槽位取代: {running['id']}（{outcome}）",
            run_id=str(running["id"]),
            key=f"{row['id']}:cancel:{int(slot.timestamp())}",
        )
        logger.info(
            "定时触发取消旧 run: workflow=%s old_run=%s 结果=%s slot=%s",
            row["workflow_id"],
            running["id"],
            outcome,
            slot.isoformat(),
        )

    try:
        run_id, created = runs_store.create_run(
            tenant_id=row["tenant_id"],
            workflow_id=row["workflow_id"],
            version=int(row["version"]),
            package_ref=row["package_ref"],
            subject={"trigger_id": row["id"], "scheduled_for": slot.isoformat()},
            # 幂等键的一部分：同一计划时刻重复投递只会落到同一个 run
            window=slot.isoformat(),
            trigger_user_id=row["owner_user_id"],
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("定时触发创建 run 失败: trigger=%s slot=%s", row["id"], slot)
        _record(row, slot, "failed", detail=str(exc)[:200], key=audit_key)
        return {
            "trigger_id": row["id"],
            "status": "failed",
            "policy": policy,
            "detail": str(exc)[:200],
            "scheduled_for": slot.isoformat(),
        }

    _record(row, slot, "consumed", detail=f"run {run_id}", run_id=run_id, key=audit_key)
    logger.info(
        "定时触发: workflow=%s run=%s scheduled=%s 新建=%s 策略=%s",
        row["workflow_id"],
        run_id,
        slot.isoformat(),
        created,
        policy,
    )
    return {
        "trigger_id": row["id"],
        "status": "consumed",
        "policy": policy,
        "run_id": run_id,
        "created": created,
        "scheduled_for": slot.isoformat(),
    }


def _dispatch(
    row: dict[str, Any], slot: datetime, *, now: datetime | None = None
) -> dict[str, Any]:
    """按计划的扇出模式分派：none 保持"一次到点一个 run"，其余走批量展开。"""
    mode = str(row.get("fanout_mode") or "none")
    if mode == "none":
        return _spawn(row, slot, now=now)
    return _spawn_batch(row, slot, now=now)


def _expand_subjects(row: dict[str, Any]) -> list[dict[str, Any]]:
    """按 fanout_mode 展开主体列表。

    - `tenant_active`：本租户 status='active' 的主体；
    - `static_list`：按显式名单（external_ref 或平台 id）解析，解析不到的跳过；
    - 其余：调用方不会走到这里。
    """
    mode = str(row.get("fanout_mode") or "none")
    if mode == "tenant_active":
        return users_store.list_active_users(row["tenant_id"])
    if mode == "static_list":
        spec = row.get("fanout_spec") or {}
        refs = spec.get("user_ids") if isinstance(spec, dict) else []
        return users_store.resolve(row["tenant_id"], list(refs or []))
    return []


def _spawn_batch(
    row: dict[str, Any], slot: datetime, *, now: datetime | None = None
) -> dict[str, Any]:
    """为一个计划时刻按主体展开建 N 个 run（P5-T03/T04/T05）。

    与 `_spawn` 的差异：
    - 一个主体一个 run：`subject.user_id` 与 `window` 都带主体，重复投递只合并同一个人；
    - 重叠判定按主体，一个用户卡住不影响其他用户；
    - 一批只写一条审计（1000 个用户不能写 1000 行触发事件）；
    - 超限（`fanout_limit`）或超总预算（`AO_FANOUT_BUDGET_CAP`）整批拒绝，不截断。
    """
    now = now or datetime.now(UTC)
    policy = _policy(row.get("overlap_policy"))
    limit = _bounded(row.get("fanout_limit"), 1000, low=1, high=MAX_FANOUT_LIMIT)
    per_run = float(row.get("budget_per_run") or 0)
    audit_key = f"{row['id']}:{int(slot.timestamp())}:fanout"

    subjects = _expand_subjects(row)
    if not subjects:
        detail = f"扇出主体为空（mode={row.get('fanout_mode')}）"
        _record(
            row,
            slot,
            "skipped",
            detail=detail,
            key=audit_key,
            extra={"fanout_mode": row.get("fanout_mode"), "total": 0},
        )
        logger.warning("扇出主体为空: trigger=%s workflow=%s", row["id"], row["workflow_id"])
        return {
            "trigger_id": row["id"],
            "status": "skipped",
            "policy": policy,
            "detail": detail,
            "scheduled_for": slot.isoformat(),
        }

    # 超限一律拒绝整批：截断会让业务方以为"所有用户都跑了"
    if len(subjects) > limit:
        detail = f"fanout_limit_exceeded: {len(subjects)}>{limit}"
        _record(
            row,
            slot,
            "failed",
            detail=detail,
            key=audit_key,
            extra={"fanout_mode": row.get("fanout_mode"), "total": len(subjects), "limit": limit},
        )
        logger.warning("扇出超限，整批拒绝: trigger=%s %s", row["id"], detail)
        return {
            "trigger_id": row["id"],
            "status": "failed",
            "policy": policy,
            "detail": detail,
            "scheduled_for": slot.isoformat(),
        }

    cap = settings.fanout_budget_cap
    if cap > 0 and per_run > 0 and len(subjects) * per_run > cap:
        detail = f"fanout_budget_cap_exceeded: {len(subjects)}x{per_run}>{cap}"
        _record(
            row,
            slot,
            "failed",
            detail=detail,
            key=audit_key,
            extra={"fanout_mode": row.get("fanout_mode"), "total": len(subjects)},
        )
        logger.warning("扇出超总预算，整批拒绝: trigger=%s %s", row["id"], detail)
        return {
            "trigger_id": row["id"],
            "status": "failed",
            "policy": policy,
            "detail": detail,
            "scheduled_for": slot.isoformat(),
        }

    downgraded = policy == "buffer_one"
    if downgraded:
        # pending_fire_at 是单槽位，按主体缓冲需要新表（B 期之后）；本期显式降级并留痕
        policy = "skip"
        logger.warning("扇出不支持 buffer_one，降级为 skip: trigger=%s", row["id"])

    created = skipped = failed = 0
    sample: list[str] = []
    first_error = ""
    for subject in subjects:
        uid = str(subject.get("external_ref") or subject["id"])
        try:
            running = _running_run(row["workflow_id"], uid)
            if running:
                if policy == "cancel_other":
                    run_state.request_cancel(
                        str(running["id"]), actor="trigger:fanout_cancel_other"
                    )
                else:
                    skipped += 1
                    logger.debug(
                        "扇出跳过（该主体有在跑的 run）: trigger=%s user=%s run=%s",
                        row["id"],
                        uid,
                        running["id"],
                    )
                    continue
            run_id, did_create = runs_store.create_run(
                tenant_id=row["tenant_id"],
                workflow_id=row["workflow_id"],
                version=int(row["version"]),
                package_ref=row["package_ref"],
                subject={
                    "trigger_id": row["id"],
                    "scheduled_for": slot.isoformat(),
                    "user_id": uid,
                    "platform_user_id": subject["id"],
                },
                # 幂等键带主体：同一时刻不同用户互不影响，同一个人重复投递只落一个 run
                window=f"{slot.isoformat()}#{uid}",
                trigger_user_id=subject["id"],
                budget_usd=per_run,
            )
            if did_create:
                created += 1
            else:
                # 幂等键命中：同一主体同一时刻已经有 run，不算新建
                skipped += 1
            if len(sample) < 5:
                sample.append(run_id)
            logger.debug(
                "扇出建 run: trigger=%s user=%s run=%s 新建=%s",
                row["id"],
                uid,
                run_id,
                did_create,
            )
        except Exception as exc:  # noqa: BLE001
            # 单主体失败不影响其他用户；但不在循环里逐条打 error（1000 用户会冲爆日志）
            failed += 1
            if not first_error:
                first_error = str(exc)[:200]

    status = "consumed" if created else ("failed" if failed else "skipped")
    detail = f"扇出 {len(subjects)} 个主体：新建 {created}，跳过 {skipped}，失败 {failed}"
    if downgraded:
        detail += "；buffer_one 在扇出下降级为 skip"
    payload = {
        "fanout_mode": row.get("fanout_mode"),
        "total": len(subjects),
        "created": created,
        "skipped": skipped,
        "failed": failed,
        "limit": limit,
        "run_ids_sample": sample,
    }
    if first_error:
        payload["first_error"] = first_error
    _record(row, slot, status, detail=detail, key=audit_key, extra=payload)
    logger.info(
        "扇出完成: workflow=%s slot=%s 总数=%s 新建=%s 跳过=%s 失败=%s",
        row["workflow_id"],
        slot.isoformat(),
        len(subjects),
        created,
        skipped,
        failed,
    )
    if failed:
        logger.error(
            "扇出存在失败主体: trigger=%s 失败数=%s 首个错误=%s",
            row["id"],
            failed,
            first_error,
        )
    return {
        "trigger_id": row["id"],
        "status": status,
        "policy": policy,
        "created": created,
        "skipped": skipped,
        "failed": failed,
        "run_ids": sample,
        "scheduled_for": slot.isoformat(),
    }


def _running_run(workflow_id: str, user_id: str | None = None) -> dict[str, Any] | None:
    """取未结束的 run；给了 user_id 就只在该主体范围内找（扇出用）。"""
    if user_id is None:
        return fetch_one(
            "SELECT id, status FROM runs WHERE workflow_id = %s"
            " AND status IN ('queued','running') ORDER BY created_at LIMIT 1",
            (workflow_id,),
        )
    return fetch_one(
        "SELECT id, status FROM runs WHERE workflow_id = %s AND subject->>'user_id' = %s"
        " AND status IN ('queued','running') ORDER BY created_at LIMIT 1",
        (workflow_id, str(user_id)),
    )


def _drain_pending(*, now: datetime | None = None, limit: int = 20) -> list[dict[str, Any]]:
    """消费 `buffer_one` 缓冲：上一次结束后把挂起的槽位拉起来。

    每轮最多处理 `limit` 条；有 run 在跑就原样保留（继续等），超时未消费则丢弃，
    避免某个卡死的 run 让缓冲永久挂着、把后续计划全堵死。
    """
    now = now or datetime.now(UTC)
    rows = fetch_all(
        "SELECT * FROM triggers WHERE enabled AND type = 'cron'"
        " AND pending_fire_at IS NOT NULL ORDER BY pending_fire_at LIMIT %s",
        (limit,),
    )
    results: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        slot = row["pending_fire_at"]
        since = row.get("pending_since") or slot
        if now - since > timedelta(seconds=PENDING_TTL_SECONDS):
            if _clear_pending(row["id"], slot):
                detail = f"缓冲超过 {PENDING_TTL_SECONDS // 3600} 小时未消费，丢弃"
                _record(row, slot, "skipped", detail=detail)
                logger.warning(
                    "定时触发缓冲过期: workflow=%s trigger=%s slot=%s",
                    row["workflow_id"],
                    row["id"],
                    slot.isoformat(),
                )
                results.append(
                    {
                        "trigger_id": row["id"],
                        "status": "skipped",
                        "detail": detail,
                        "scheduled_for": slot.isoformat(),
                    }
                )
            continue
        if _running_run(row["workflow_id"]):
            continue  # 上一次还没结束，缓冲继续等
        if not _clear_pending(row["id"], slot):
            continue  # CAS 失败：别的调度器已消费
        results.append(_spawn(row, slot, now=now))
    return results


def _clear_pending(trigger_id: str, slot: datetime) -> bool:
    """CAS 清空缓冲；返回是否由本次调用清掉（防止多副本重复拉起）。"""
    with tx() as conn:
        updated = conn.execute(
            "UPDATE triggers SET pending_fire_at = NULL, pending_since = NULL"
            " WHERE id = %s AND pending_fire_at = %s",
            (trigger_id, slot),
        ).rowcount
    return bool(updated)


def backfill(
    *,
    workflow_id: str,
    trigger_id: str,
    start: datetime,
    end: datetime,
    limit: int = DEFAULT_BACKFILL_LIMIT,
    now: datetime | None = None,
) -> dict[str, Any]:
    """手动补跑：把 `[start, end]` 内该 cron 计划的槽位逐个建成 run。

    与正常触发的关系：
    - 幂等键完全一致（`window=槽位`、`subject` 含 trigger_id），所以补跑过的时刻
      不会因为 scheduler 又扫到而重复跑；
    - 不改 `next_fire_at`——补历史不影响未来计划；
    - 忽略重叠检查（对齐 Temporal 建议的 AllowAll/BufferAll 回填姿势），否则回填
      会被"当前有 run 在跑"全部吃掉，等于没补。

    参数问题抛 ValueError（Web 层转 400），计划不存在抛 LookupError（转 404）。
    """
    now = now or datetime.now(UTC)
    raw = fetch_one(
        "SELECT * FROM triggers WHERE id = %s AND workflow_id = %s", (trigger_id, workflow_id)
    )
    if raw is None:
        raise LookupError("触发计划不存在")
    row = dict(raw)
    if row["type"] != "cron" or not row["cron"]:
        raise ValueError("只有 cron 计划支持回填")
    if not row["enabled"]:
        raise ValueError("计划已失效（重新发布后旧的计划不能回填）")
    if end <= start:
        raise ValueError("end 必须晚于 start")
    if (end - start).total_seconds() > MAX_BACKFILL_SPAN:
        raise ValueError("回填区间最长 31 天")

    limit = max(1, min(MAX_BACKFILL_SLOTS, int(limit)))
    slots: list[datetime] = []
    # next_fire 严格取 after 之后，减 1 微秒才能把 start 本身命中
    cursor = next_fire(row["cron"], row["timezone"], start - timedelta(microseconds=1))
    while cursor <= end:
        if len(slots) >= limit:
            # 不做静默截断：截断会让"补一周"变成"补前 N 个"，结果不可预期
            raise ValueError(
                f"区间内槽位超过 {limit} 个（单次上限 {MAX_BACKFILL_SLOTS}），请缩小范围"
            )
        slots.append(cursor)
        cursor = next_fire(row["cron"], row["timezone"], cursor)
    if not slots:
        raise ValueError("区间内没有符合条件的触发时刻")

    results = [
        _spawn(
            row,
            slot,
            ignore_overlap=True,
            audit_key=f"bf:{row['id']}:{int(slot.timestamp())}",
            now=now,
        )
        for slot in slots
    ]
    summary = {
        "trigger_id": row["id"],
        "workflow_id": workflow_id,
        "planned": len(slots),
        "created": sum(1 for item in results if item.get("created")),
        "failed": sum(1 for item in results if item["status"] == "failed"),
        "run_ids": [item.get("run_id") for item in results if item.get("run_id")],
        "scheduled_for": [item["scheduled_for"] for item in results],
    }
    logger.info(
        "手动回填: workflow=%s trigger=%s 区间=%s~%s 槽位=%s 新建=%s 失败=%s",
        workflow_id,
        row["id"],
        start.isoformat(),
        end.isoformat(),
        summary["planned"],
        summary["created"],
        summary["failed"],
    )
    return summary


def _record(
    row: dict[str, Any],
    moment: datetime,
    status: str,
    *,
    detail: str = "",
    run_id: str | None = None,
    key: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """写触发审计；默认以计划时刻为粒度，重复扫描会覆盖同一条。

    `extra` 用于扇出汇总（total/created/skipped/failed 等）——一批只落一行，
    单用户结果查 `runs.subject->>'user_id'`，不往审计表里写 N 行。
    """
    event_key = key or f"{row['id']}:{int(moment.timestamp())}"
    payload: dict[str, Any] = {"detail": detail, "scheduled_for": moment.isoformat()}
    if run_id:
        payload["run_id"] = run_id
    if extra:
        payload.update({k: v for k, v in extra.items() if v is not None})
    with tx() as conn:
        conn.execute(
            "INSERT INTO trigger_events (id, workflow_id, trigger_id, event_key, payload, status)"
            " VALUES (%s,%s,%s,%s,%s,%s)"
            " ON CONFLICT (event_key) DO UPDATE SET"
            " status = EXCLUDED.status, payload = EXCLUDED.payload",
            (new_id("tev"), row["workflow_id"], row["id"], event_key, Jsonb(payload), status),
        )


def _field(item: Any, name: str) -> Any:
    return item.get(name) if isinstance(item, dict) else getattr(item, name, None)


def _policy(raw: Any) -> str:
    """重叠策略兜底：未知值一律按 skip，绝不放宽重叠保护。"""
    value = str(raw or "").strip()
    return value if value in OVERLAP_POLICIES else "skip"


def _normalize_fanout(raw: Any) -> dict[str, Any]:
    """把 manifest 的 fanout 段归一成 DB 四列（mode/spec/limit/budget）。

    manifest 形态：
        {"mode": "tenant_active", "limit": 1000, "budget_per_run": 0.5}
        {"mode": "static_list", "user_ids": ["u1", "u2"]}
    `spec` 只保留当前模式用得到的部分，避免把无关字段写进 DB 后被误读。
    """
    data = raw if isinstance(raw, dict) else {}
    mode = str(data.get("mode") or "none").strip() or "none"
    if mode not in {"none", "tenant_active", "static_list"}:
        # 发布期应已拒绝；这里兜底回 none，绝不让未知模式走到建 run
        logger.warning("未知扇出模式，回落 none: %s", mode)
        mode = "none"
    try:
        budget = float(data.get("budget_per_run") or 0)
    except (TypeError, ValueError):
        budget = 0.0
    spec: dict[str, Any] = {}
    if mode == "static_list":
        spec["user_ids"] = [
            str(ref).strip() for ref in (data.get("user_ids") or []) if str(ref).strip()
        ]
    return {
        "mode": mode,
        "spec": spec,
        "limit": _bounded(data.get("limit"), 1000, low=1, high=MAX_FANOUT_LIMIT),
        "budget": max(0.0, budget),
    }


def _bounded(raw: Any, default: int, *, low: int, high: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))
