# P3 · 控制面与大盘（M4 / M6）

> **迁移说明**（2026-11-19 自 `auto-orchestration-platform/docs/superpowers/plans/` 迁入）
> 正文保留原文未改。文中 `docs/superpowers/{research,specs,experiments,decisions}/...`
> 形式的路径指向**原仓库**，未随迁；已落地文档在 `dsh-platform/docs/{research,reports}/`。
> 本文件是**事前规格**（要做什么、如何验收），实际执行结果与偏离记录见 `docs/STATUS.md`。

日期：2026-11-13
上游：`2026-11-13-p1-execution-kernel.md`（队列/状态机/记账）、`2026-11-13-p2-workflow-generation.md`（触发器与发布事件）
覆盖：M4 控制面与大盘、M6 1000 任务压测
前置：P1 全部退出条件达成（尤其 P1-T05 槽位实测、P1-T07 租约、P1-T12 网关记账）
估时：12–16 人日

## 1. 目标与退出条件

**目标**：运营能"看得到、停得下、查得清账"，并且 1000 个任务一小时内全部有终态。

退出条件：

1. 大屏五级聚合可下钻：租户 → 工作流版本 → 批次 → run → node；详情到 `mcp__<server>__<tool>` 级。
2. cron 触发一次 fanout 1000 个主体 → 1 小时内 100% 终态；`queued` 深度曲线可见；429 有计数且不丢任务。
3. 成本三处对账一致：网关 spend log、`llm_usage`、`runs.spent_usd`（逐请求可对，聚合误差 0）。
4. `operator` 改不了 `global` 资源（接口 403 + 审计有记录）；`admin` 代改他人资源带目标 owner。
5. 观察者问答能回答"卡在哪、花了多少、调了哪个工具"，且不计入 run 预算。
6. 事件表按天分区，30 天前数据可归档到对象存储并从查询侧透明降级。

## 2. 起点与终点

| 维度 | MVP 现状 | P3 终点 |
|---|---|---|
| 刷新 | 3s meta refresh | SSE，断线可续（`Last-Event-ID`） |
| 触发 | 只有手动 `POST /workflows/{id}/trigger` | cron / event / scan / API，统一幂等与 fanout |
| 配额 | 无 | 租户 × 并发 / 工作流 × 并发 / 租户日金额与 token |
| 成本 | run 级花费 | 六维聚合 + 报表 + 对账告警 |
| 审计 | `run_events` | 可按 actor / 对象 / 时间查，含 `operator.action` |
| 权限 | 角色 + 最后 admin 保护 | 补 API Key、租户、资源归属校验、跨租户只读 |
| 大盘 | 列表 + 详情 | 列表指标 + 节点时间线 + 观察者问答 |
| 压测 | 无 | 1000 任务报告 + 容量结论 |

## 3. 前提决策

| # | 决策 | 倾向 | ADR |
|---|---|---|---|
| D11 | SSE 实现 | `sse-starlette`，事件源为 `run_events` 轮询（500ms）+ PG `LISTEN/NOTIFY` 可选优化 | `ADR-010-sse.md` |
| D12 | 调度器形态 | 单实例 `scheduler` 容器 + PG advisory lock；不做选主 | `ADR-011-scheduler.md` |
| D13 | 公平调度 | 租户轮转 FIFO（按 `tenant_id` 分桶，桶内 `created_at`），不是全局 FIFO | `ADR-012-fair-queue.md` |
| D14 | 报表存储 | 明细只存 `llm_usage`；日聚合用物化视图（`mv_usage_daily`），不做双写 | `ADR-013-cost-report.md` |

## 4. 任务清单

### P3-T01 业内方案复核

产出 `docs/superpowers/research/2026-11-13-control-plane.md`：

- 单机 cron 调度与 advisory lock 的选型（APScheduler vs 自写扫描 loop），失败补偿做法。
- SSE 在反向代理后的断线重连与事件续传标准做法（`Last-Event-ID`、心跳间隔）。
- LLM 成本归因的行业做法（虚拟 key / 请求标签 / 网关流水对账）。
- 队列公平调度（DRR / 租户轮转）在 PG 上的可行写法。

工具：`agent-reach`、`github-search`；结论附来源。

### P3-T02 用户、角色、API Key、租户配额

改动：`src/ao/auth.py`、新增 `src/ao/api_keys.py`、管理页。

- API Key：`sk_` 前缀、明文只返回一次、库存 sha256 + 前缀、有效期按日历月（1/3/6/12，默认 1）、`last_used_at` 60s 节流。
- 账号状态 `pending/active/rejected`：自助注册只能建 `operator` 且 `pending`；状态变更后已签发 token 立即失效。
- 最后一个 `admin` 不可降级/删除；用户可注销自己并清 token。
- 租户：`tenants(quota_daily_usd, quota_daily_tokens)`，admin 可改。
- 所有写操作过 `require_role` + `assert_owner`，落 `operator.action`。

验收：`tests/integration/test_auth.py`：pending 用户 token 立即失效；operator 改 global 403；删最后 admin 409。

### P3-T03 触发器框架

新增 `src/ao/triggers/`（`cron.py` / `event.py` / `scan.py` / `api.py`）+ `src/ao/scheduler.py`。

- `cron`：单实例，advisory lock；触发前算 fanout 主体列表（`query.users_due_today`），一个主体一个 run。
- `event`：写 `trigger_events` 表，scheduler 扫未处理事件；处理幂等（`event_id` 唯一）。
- `scan`：按时间窗扫描业务库（只读），产出主体列表。
- `api`：`POST /api/workflows/{id}/trigger`（API Key 鉴权）。
- 幂等键：`sha1(workflow_id + version + subject + 触发窗口)`；重复触发返回已有 run，不新建。
- 时区：manifest 里 `timezone` 必填，默认 `Asia/Shanghai`；DST 边界用 UTC 计算。

验收：`tests/integration/test_triggers.py`：同一 cron 窗口两次触发只产生一批；事件重复消费不重复建 run。

### P3-T04 背压、配额与公平调度

改动：`src/ao/queue.py`。

- 三层配额：租户 × 并发 run、工作流 × 并发 run、租户日金额/token；先到先限。
- 超限 run 停 `queued` 并写排队原因（`queue_reason`），**不拒绝创建**。
- `queued` 深度超阈值 → 暂停 fanout + 告警事件。
- 公平：按租户轮转取任务（PG 侧用窗口函数或两级查询，实现写进 ADR）。
- 扩容信号：队列深度 + 429 率 + 依赖 P95，不看 CPU。

验收：`tests/integration/test_quota.py`：大租户 500 个 run 排队时，小租户 1 个 run 在 5s 内被领走。

### P3-T05 SSE 与详情页

改动：`src/ao/web.py`、`web/templates/run_detail.html`。

- `GET /api/runs/{id}/events/stream`（SSE）：先补发 `Last-Event-ID` 之后的事件，再增量推送；15s 心跳。
- 详情页：
  - 节点时间线：每节点模型、token、耗时、`mcp`/`skill`、tool 调用序列、产物链接。
  - `meta` 区：所有降级标记（`system_injected` / `agent_fallback` / `sandbox_fallback`）。
  - 顶部状态条：`queued` 时显示排队原因与位次。
- 列表页指标：状态分布、排队深度、今日失败、今日花费、429 率、最老任务年龄。

验收：`tests/e2e/test_sse.py`：断开重连不丢事件；详情页 1s 内看到 `node.started`。

### P3-T06 观察者问答

新增 `src/ao/observer.py` + 详情页右侧面板。

- 上下文 = 图状态 + 最近 50 条事件 + 节点 `outputs` 摘要 + 已绑 MCP/skill + 已花成本，**不含完整 transcript**。
- 实现：一次普通 LLM 调用（不带工具），不占执行池槽位。
- 需要看文件时升级为只读 DSH 会话（`read_only` 能力包 + 包目录只读）。
- 计费 `platform.observe`，不计入 run 预算；问答内容落事件表可审计。

验收：`tests/integration/test_observer.py`：问答上下文不含 transcript；成本记在 `platform.observe`；升级路径产生只读会话且无写事件。

### P3-T07 成本报表与对账

新增 `src/ao/reports.py` + 页面。

- 六维：租户 / 用户（`trigger_user_id`）/ 工作流版本 / run / 节点 / 单次请求。
- 计价区分 token 类型：缓存读 0.1×、缓存写 1.25×、普通输入 1×（口径来自网关）。
- 日聚合物化视图 `mv_usage_daily`，按需 `REFRESH MATERIALIZED VIEW CONCURRENTLY`。
- 对账任务：拉网关 spend log 与 `llm_usage` 比对，差异 > 2% 告警。
- 导出 CSV（按当前筛选）。

验收：`tests/integration/test_reconcile.py`：造 3 条差异 → 告警事件；0 差异 → 无告警；报表合计 = `SUM(llm_usage.cost)`。

### P3-T08 审计查询与操作留痕

- `GET /audit?actor=&object=&from=&to=`：查 `run_events` + `operator.action`。
- 所有写操作（停/重试/放行预算/建分享/改绑定/发布/克隆）必须落 `operator.action`，带 `actor`、目标对象、`scope`、`scope_id`。
- 审计只读，不提供删除入口（归档由 P3-T09 负责）。

验收：`tests/integration/test_audit.py`：operator 停止他人 run → 审计可查；越权尝试也落 `operator.action`（`result=denied`）。

### P3-T09 事件分区与归档

改动：`migrations/0NN_run_events_partition.sql`。

- PG 声明式分区：按天（`created_at`），默认分区兜底。
- 保留 30 天；归档任务把过期分区导出到对象存储（Parquet 或 JSONL.gz），验证可读后 `DETACH` + `DROP`。
- 查询侧透明：详情页超出保留期提示"已归档，需申请调取"。

验收：`tests/integration/test_partition.py`：跨天插入落到正确分区；归档后原表查不到、对象存储可读。

### P3-T10 1000 任务压测（M6）

新增 `scripts/loadtest.py` + `docs/superpowers/experiments/2026-xx-loadtest-1000.md`。

- burst 1000 个 run（同一工作流 × 1000 主体），记录：入队耗时、首节点延迟、P50/P95 节点耗时、`queued` 深度曲线、429 计数、终态时间分布。
- 限流：共享令牌桶（每模型一个，速率 = 供应商 TPM × 80%）；超限节点回 `queued`，**不在 worker 内 sleep**。
- 通过标准：1 小时内 100% 终态；无丢任务；三处账目一致；worker RSS 不超预算。
- 不通过时的处置顺序：加槽位 → 拆节点 → 降 max_turns；不改终态定义。

验收：报告给出结论与下一次扩容信号阈值。

## 5. 与相邻线的接口

| 交给谁 | 内容 |
|---|---|
| P1 | 配额检查在 claim 前，超限不改 run 状态；429 计数进 `run_events` |
| P2 | 触发器只认已发布版本；`workflow.published` 事件触发冒烟订阅重跑 |
| P4 | 页面数据源 `run.list` / `run.get` / `run.events` 直接复用本线的查询层（带 scope 过滤） |

## 6. 风险与对策

| 风险 | 对策 |
|---|---|
| 1000 任务压测不达标 | 先按 P1-T05 实测加槽位，再拆节点；不允许靠放宽验收过关 |
| cron 重复触发 | advisory lock + 幂等键双保险；重复触发返回已有 run |
| SSE 连接数过多 | 每页一个连接；详情页离开即断开；服务端按 run 分组合并轮询 |
| 报表查询拖慢主库 | 物化视图 + 只读副本（第二台机器先上 replica） |
| 事件表膨胀 | 分区 + 30 天归档；写入只 append，不更新 |
| 观察者问答被当成"读全量日志" | 上下文白名单固定；升级只读会话需显式点"深入查看" |

## 7. 本线不做

- 自动扩缩容（扩容只加 worker，人工执行）。
- 跨区域多活、PG 自动 promote。
- 外部客户计费/充值。
- 自由画布页面编辑（P4）。
