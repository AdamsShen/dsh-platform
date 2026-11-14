# P2-T19 重叠策略扩展 + 手动 backfill

## 1. 需求背景

P2-T17/T18 只实现了"重叠默认跳过"（Temporal 的 Skip）：上一次 run 还在 `queued/running` 时，
到点或事件触发的槽位一律记 `skipped`，不排队也不取消。缺口有两个：

1. **重叠策略不可选**。业务上"日报"适合跳过，"对账"希望补跑一次（BufferOne），
   "实时监控"希望新的覆盖旧的（CancelOther），目前都做不到。
2. **没有手动补跑入口**。停机、改 cron、排障后想按时间区间补历史时刻，只能改表
   `next_fire_at` 再等扫描，既危险也不可审计。

`docs/STATUS.md` 中 P2-T19 即本项。

## 2. 业内调研（2026-11-14 检索）

### Temporal Schedules

`ScheduleOverlapPolicy` 枚举定义（[Temporal Python SDK](https://python.temporal.io/temporalio.client.ScheduleOverlapPolicy.html)）：

| 策略 | 语义 |
|---|---|
| SKIP | 不启动；等当前 run 结束后，只考虑该时刻之后的下一个计划点 |
| BUFFER_ONE | 当前在跑时缓冲 **一个** 启动；缓冲已满则新的丢弃 |
| BUFFER_ALL | 缓冲任意多个，当前结束后按序全部启动 |
| ALLOW_ALL | 允许任意并发 |
| CANCEL_OTHER | 取消当前在跑的（cancel 语义，等其响应取消），再启动新的 |
| TERMINATE_OTHER | 强制终止当前在跑的（terminate 语义，不给收尾机会），再启动新的 |

Backfill 是独立 API（[temporal schedule backfill](https://docs.temporal.org.cn/cli/schedule)）：
`--start-time / --end-time / --overlap-policy`，在指定区间内按计划批量启动；官方建议回填时
配 `BufferAll` 或 `AllowAll`，否则会被重叠策略吃掉、回填等于没做。

### Kubernetes CronJob

`spec.concurrencyPolicy` 三值（[K8s 文档](https://deploy-preview-51899--kubernetes-io-main-staging.netlify.app/zh-cn/docs/concepts/workloads/controllers/cron-jobs)）：
`Allow`（并发）/ `Forbid`（禁止，跳过本次）/ `Replace`（杀掉旧的换新的）。
与我们已有语义对照：`Forbid ≈ skip`、`Replace ≈ cancel_other`。

### Airflow

`max_active_runs` + `catchup`：并发上限到了就把待跑实例留在 scheduler 里排队（行为接近 BufferAll，
且是持久化队列而非内存缓冲）。

### 归纳与本项目的取舍

- 主流都区分"**不启动 / 缓冲 / 覆盖**"三类，且缓冲策略明确"满则丢弃"，不做无界排队
  ——无界排队会把停机期间的计划变成雪崩。
- 覆盖类分"优雅取消"与"强制终止"两档。本项目停止只在节点边界生效（P1-T09 的设计约束，
  DSH 单轮无 cancel），因此只提供 **CANCEL_OTHER（置 `cancel_requested`，节点边界退出）**，
  不提供 TERMINATE_OTHER——强杀与我们的"不静默中断"原则冲突。
- 缓冲用**数据库列**（`pending_fire_at`）而不是内存队列：调度器无状态、可多副本，重启不丢。
- 手动 backfill 对齐 Temporal：独立入口、区间参数、回填时强制放开重叠检查，并在审计表留痕。
- 不引入 ALLOW_ALL 作为常规策略（我们没有 run 并发上限，容易打爆队列），
  仅 backfill 内部使用"忽略重叠"。

## 3. 设计

### 3.1 迁移 `migrations/009_overlap_and_backfill.sql`

```sql
ALTER TABLE triggers ADD COLUMN overlap_policy TEXT NOT NULL DEFAULT 'skip'
  CHECK (overlap_policy IN ('skip','buffer_one','cancel_other'));
ALTER TABLE triggers ADD COLUMN pending_fire_at TIMESTAMPTZ;
ALTER TABLE triggers ADD COLUMN pending_since  TIMESTAMPTZ;
CREATE INDEX triggers_pending ON triggers (pending_fire_at)
  WHERE enabled AND pending_fire_at IS NOT NULL;
-- trigger_events.status 增加 buffered / cancelled
```

`pending_fire_at` 是"缓冲槽位"，`pending_since` 用于兜底过期（见 3.3）。

### 3.2 语义

| 策略 | 有 run 在跑时 |
|---|---|
| `skip`（默认，现状） | 记 `skipped`，指针照常推进，不排队 |
| `buffer_one` | 缓冲空 → 写入 `pending_fire_at=槽位`，记 `buffered`；缓冲已满 → 记 `skipped`（"缓冲已满"），**不覆盖**（对齐 Temporal BUFFER_ONE） |
| `cancel_other` | 对在跑 run 调 `state.request_cancel(actor="trigger:cancel_other")`，随后立即创建新 run；旧 run 在节点边界退出 |

### 3.3 缓冲的消费

`fire_due()` 每轮先 `_drain_pending()` 再扫到期计划：

- 按 `pending_fire_at` 升序取（每轮 20 条），有 run 在跑 → 保留等待；
- 无 run 在跑 → CAS 清空缓冲并创建 run（幂等键与正常触发相同：`window=槽位`，
  `subject={"trigger_id","scheduled_for"}`，因此重复投递不会多跑）；
- `pending_since` 超过 24h 仍未消费 → 清空并记 `skipped`（防止某个卡死的 run 把缓冲永久挂住）。

缓冲只影响 run 创建，不改 `next_fire_at` 指针。

### 3.4 手动 backfill

`POST /api/workflows/{workflow_id}/triggers/{trigger_id}/backfill`

```json
{"start": "2026-11-01T00:00:00Z", "end": "2026-11-07T00:00:00Z", "limit": 50}
```

- 鉴权复用事件入站的 `X-AO-Event-Token`（未配置时仅 `AO_ENV=dev` 放行）；
- trigger 必须属于该工作流、`type='cron'`、`enabled`，否则 404；
- 区间必须 `end > start`，跨度 ≤ 31 天；命中槽位 > `limit`（默认 50，上限 200）直接 400，
  不做静默截断（截断会让"补了一周"变成"补了前 50 个"，结果不可预期）；
- 每个槽位按 **忽略重叠** 创建 run（对齐 Temporal 建议的 AllowAll/BufferAll），
  幂等键与正常触发一致 → 同一时刻已跑过则返回 `created=false`；
- **不改** `next_fire_at`，backfill 是补历史，不影响未来计划；
- 审计 `trigger_events.event_key = bf:{trigger_id}:{槽位时间戳}`，逐槽位记 `consumed/failed`。

### 3.5 配置透传

- `manifest.Trigger.overlap_policy: str = "skip"`，`parse()` 校验非法值（`E_TRIGGER_OVERLAP`）；
- 草稿页下拉框 → `draft.apply_triggers(overlap_policy=...)` → `orchestrator.update_draft` → 发布投影；
- 页面展示已生效策略与缓冲状态；已发布时给出 backfill 的 curl 示例。

## 4. 文件清单

| 文件 | 改动 |
|---|---|
| `migrations/009_overlap_and_backfill.sql` | 新增列、约束、索引、审计状态值 |
| `src/ao/triggers.py` | `project` 落策略；`_spawn` 分派三种策略；新增 `_drain_pending` / `backfill` |
| `src/ao/manifest.py` | `Trigger.overlap_policy` + 校验 |
| `src/ao/planner/draft.py` | `apply_triggers` 接收并校验策略 |
| `src/ao/planner/orchestrator.py` | `update_draft` 透传策略 |
| `src/ao/web/app.py` | 表单透传、backfill 路由、页面上下文（策略/缓冲） |
| `src/ao/web/templates/planner.html` | 策略下拉、已生效展示、backfill 提示 |
| `tests/e2e/test_triggers.py` | buffer_one / cancel_other / 缓冲消费与过期 / backfill 幂等与越界 / 清单校验 |
| `docs/STATUS.md` | P2-T19 标完成 |

## 5. 备选方案

| 方案 | 取舍 |
|---|---|
| 用内存队列做 BufferOne | 调度器多副本/重启会丢缓冲，且 `scheduler.py` 已设计为无状态 → 否决 |
| 缓冲按"最新槽位覆盖"而非"满则丢弃" | 更符合直觉，但与 Temporal BUFFER_ONE 语义不一致；想补最新时刻应走 backfill → 否决，注释说明 |
| backfill 走异步任务表 | 区间大时可避免请求超时，但当前上限 200 个槽位、单次插入毫秒级 → 同步足够，异步留待需要时 |
| 提供 TERMINATE_OTHER / ALLOW_ALL | 前者与"节点边界停止"冲突，后者无并发上限保护 → 不提供 |

## 6. 状态

- [x] 调研（Temporal / K8s / Airflow）
- [x] 迁移 009
- [x] triggers / manifest / draft / orchestrator
- [x] Web 表单与 backfill 接口
- [x] 测试（新增 8 条）与 STATUS 更新：`ruff` 通过，`pytest` 104 passed
