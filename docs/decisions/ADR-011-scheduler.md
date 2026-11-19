# ADR-011 调度器形态：单实例循环 + advisary lock + CAS 推进

- 状态：已实现
- 日期：2026-11-13（记录补写于 2026-11-19）
- 相关：P3-T03 的前半（已提前实现为 P2-T17~T19）；`src/ao/scheduler.py`、`src/ao/triggers.py`

## 背景

需要有人在后台做三件事：回收过期租约、到点触发 cron/event、同步网关 spend log。多副本部署时不能出现两个调度器同时推进同一个 `next_fire_at`。

## 决策

1. **单实例调度循环**：`scheduler.run_forever(interval=10.0)`（`src/ao/scheduler.py:79`），一个进程内串行执行。
2. 用 **PG advisory lock** 保证同库只有一个调度器生效：`try_lock()` → `pg_try_advisory_lock(key)`（`scheduler.py:21`）。不做选主、不做租约。
3. 到点推进用 **CAS**：`UPDATE triggers SET next_fire_at = ... WHERE id = ? AND next_fire_at = ?`，抢占失败即跳过本次（多副本安全，即使锁失效也不会重复触发）。
4. 触发与租约回收**同一循环**（`scheduler.py:44` 起），不引入第二个后台线程。
5. 默认重叠策略 `skip`（对齐 Temporal Skip），不补跑错过时刻；补跑走显式 backfill 接口（`triggers.backfill`）。

## 理由

- 调度是低频后台任务，单实例足够；advisory lock 比引入选主组件（ZooKeeper/etcd）便宜一个数量级。
- CAS 是第二道保险：即使有人误起第二个调度器，也只有一个能推进指针。
- 同循环串行执行让"回收租约 → 触发 → 同步"的顺序确定，便于排查。

## 替代方案

| 方案 | 否决原因 |
|---|---|
| APScheduler 进程内定时 | 与 CAS 的抢占语义重复；多副本仍需额外锁 |
| 独立调度服务 + 选主 | 当前规模不需要，运维面变大 |
| 每个计划一个协程/定时器 | 1000 个计划 = 1000 个定时器，重启后恢复复杂 |

## 后果

- 正向：调度器可随意重启；`next_fire_at` 就是唯一游标，宕机不丢计划。
- 负向：**单点**——调度器长时间不跑，本应触发的时刻会被跳过（默认 skip），不会自动补；需要人工 `backfill`。
- 已知边界：`buffer_one` 在扇出模式下不支持（`pending_fire_at` 是单槽位），见 P5-T04。

## 验证

- `tests/integration/test_triggers.py`：同一 cron 窗口两次触发只产生一批；事件重复消费不重复建 run。
- 实测：发布 → 改表到点 → `fire_due` 建 run → worker 跑通（`docs/STATUS.md` P2-T17 行）。
