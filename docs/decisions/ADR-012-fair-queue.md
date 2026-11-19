# ADR-012 队列公平调度：租户轮转 FIFO（未实现）

- 状态：**已决策，未实现**（P3-T04 延后；M1 扇出会先放大队列压力）
- 日期：2026-11-13 决策 / 2026-11-19 标记现状
- 相关：P3-T04、P5-T05；`src/ao/queue.py`

## 背景

当前领取是全局 FIFO：`SELECT ... WHERE status='queued' ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1`。一旦某个大租户一次扇出 500 个 run，小租户的新 run 会排在 500 个之后，出现"大租户饿死小租户"。

## 决策（尚未落地）

1. **按租户轮转**而不是全局 FIFO：桶内按 `created_at`，桶间轮转取。
2. PG 侧实现二选一（实现时定稿）：
   - 两级查询：先取"当前应服务的租户"（记录轮转指针），再从该租户桶里 `SKIP LOCKED` 取 1 条；
   - 或窗口函数：`ROW_NUMBER() OVER (PARTITION BY tenant_id ORDER BY created_at)` 取每租户第一条，再按最小 `created_at` 轮转。
3. 三层配额先在 claim **之前**检查：租户 × 并发 run、工作流 × 并发 run、租户日金额/token。
4. 超限的 run **停在 `queued` 并写 `queue_reason`**，不拒绝创建、不判失败。
5. `queued` 深度超阈值 → 暂停 fanout + 告警事件（扩容信号是队列深度 + 429 率 + 依赖 P95，不看 CPU）。

## 理由

- 租户是最自然的隔离单位：一个租户的最坏情况不该影响其他租户。
- 不引入 Redis/独立队列服务，PG 现有索引与 `SKIP LOCKED` 足够；代价是查询比单条 FIFO 复杂。
- "超限不拒绝创建"保证业务侧不丢任务，只是晚跑。

## 替代方案

| 方案 | 否决原因 |
|---|---|
| 全局 FIFO（现状） | 大租户扇出会饿死小租户，M1 之后立刻暴露 |
| 严格 DRR/加权公平 | 权重需要运营维护，当前只有"谁都不能饿死"一个诉求 |
| 独立消息队列（Redis/SQS） | 多一套有状态组件，且 run 状态仍在 PG，需双写 |

## 后果

- 正向：M1 扇出 1000 个 run 时，其他租户仍能在 5s 内被服务。
- 负向：领取查询变复杂，需要额外索引（`(tenant_id, status, created_at)`）；轮转指针若用表记录，要多一次写。
- 依赖：`runs.tenant_id` 必须可靠（当前由 workflow/trigger 派生），否则轮转无意义。
- 验收：`tests/integration/test_quota.py`——大租户 500 个排队时，小租户 1 个 run 在 5s 内被领走。

## 与其他 ADR 的关系

- 与 ADR-011（调度器）独立：调度器只负责"到点建 run"，公平性在 worker 领取侧。
- 与 M1 扇出耦合：扇出先把队列压力放大 1000 倍，`fanout_limit` 是粗粒度闸门，公平调度是细粒度闸门，两者都要。
