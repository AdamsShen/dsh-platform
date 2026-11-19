# P5：多用户与定时扇出

日期：2026-11-19
前置：P2-T17~T19（cron / event / 重叠策略，已完成）
覆盖需求：#6 多人并发、#9 定时按用户扇出、#11 用户维度费用、#10 云上部署、#14 跨机接管
估时：15–18 人日（A 期 10 人日，B 期 5–8 人日）

## 1. 需求背景

首个真实负载是「角色日程表批量生产」：**一条工作流，每天定时，为每个用户各生成一份日程表**。
当前实现只能做到「一次到点 = 一个 run = 一个 owner」：

```443:446:/Library/git-root/dsh-platform/src/ao/triggers.py
            subject={"trigger_id": row["id"], "scheduled_for": slot.isoformat()},
            window=slot.isoformat(),
            trigger_user_id=row["owner_user_id"],
```

`subject` 里没有任何业务主体维度，`window` 只有时刻——**100 个用户会被 `runs.dedupe_key` 合并成 1 个 run**。
另外 `users` 表在 `migrations/001_init.sql:12` 就建好了，但全仓无一处读写，主体列表没有来源；
`llm_usage` 也没有 user 列，用户维度费用只能 `JOIN runs.subject->>'user_id'` 且无索引。

本期目标：

1. 触发器支持**主体展开**：一次到点 → N 个独立 run，各自 subject 与幂等键；
2. 重叠策略按**主体**判定，一个用户卡住不阻塞其他用户；
3. 费用能按用户直接查，不依赖 JSONB JOIN 全表扫；
4. 包可跨机解析（多副本/接管的前置）；
5. 全程可审计：一批扇出只落一条汇总审计，不写 N 行。

## 2. 业内调研

| 来源 | 关键做法 | 对本项目的启示 |
|---|---|---|
| [Temporal Schedules](https://docs.temporal.io/evaluate/development-production-features/schedules) | 官方建议「每个实体一条 Schedule」与「一条 Schedule + 内部 fanout」两种；前者运维面随实体数线性增长，后者把展开放进业务逻辑 | 实体数会到 1000 时选**内部 fanout**：一条计划，展开在触发器里做 |
| [Celery Canvas: group / chord](https://docs.celeryq.dev/en/stable/userguide/canvas.html) | `group` 并发派发 N 个子任务，`chord` 等全部完成后回调；子任务各有独立 id | 扇出后每个 run 必须是独立可重试单元，不能是"一个大 run 跑循环" |
| [GitHub Actions matrix](https://docs.github.com/en/actions/using-jobs/using-a-matrix-for-your-jobs) | 一个 job 定义展开成 N 个并行 job，`fail-fast` 可关闭 | 展开要有**上限**与**单点失败不影响其他**两个开关 |
| 本项目 P2-T17~T19 既有实现 | 投影表 + CAS 抢 `next_fire_at` + `runs.dedupe_key` 幂等 + 默认 skip | 扇出必须复用这三件，不引入新基础设施 |

归纳：**一条计划 → 展开主体 → N 个独立 run（各自幂等键）→ 单主体失败隔离 → 展开有上限**。
不需要引入 Temporal / Celery，现有 PG 与调度循环够用。

## 3. 技术设计

### 3.1 数据（`migrations/012_fanout.sql`）

```sql
-- 扇出配置挂在计划上：一条工作流的展开方式不随 run 变化
ALTER TABLE triggers ADD COLUMN IF NOT EXISTS fanout_mode TEXT NOT NULL DEFAULT 'none';
ALTER TABLE triggers ADD COLUMN IF NOT EXISTS fanout_spec JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE triggers ADD COLUMN IF NOT EXISTS fanout_limit INTEGER NOT NULL DEFAULT 1000;
ALTER TABLE triggers ADD CONSTRAINT triggers_fanout_mode_check
  CHECK (fanout_mode IN ('none','tenant_active','static_list','mcp_query'));
ALTER TABLE triggers ADD CONSTRAINT triggers_fanout_limit_check
  CHECK (fanout_limit BETWEEN 1 AND 5000);

-- 单主体预算：扇出后 run 量 × 单 run 预算 = 一批的总支出上限
ALTER TABLE triggers ADD COLUMN IF NOT EXISTS budget_per_run NUMERIC(14,6) NOT NULL DEFAULT 0;

-- 用户维度费用：冗余一列，避免每次报表都 JOIN 扫 JSONB
ALTER TABLE llm_usage ADD COLUMN IF NOT EXISTS user_id TEXT;
CREATE INDEX IF NOT EXISTS idx_llm_usage_user ON llm_usage (user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_runs_subject_user
  ON runs ((subject->>'user_id')) WHERE subject ? 'user_id';
```

`fanout_spec` 三种形态（按 `fanout_mode` 取值）：

```jsonc
{ "scope": "tenant_active" }                                   // tenant_active：取本租户 status='active' 的用户
{ "user_ids": ["u1", "u2"] }                                    // static_list：显式名单，用于灰度/补跑
{ "mcp": { "server": "hr-db", "tool": "list_users", "args": {} } }  // mcp_query：从 MCP 现查（B 期）
```

`users` 表补一列业务主键，避免把平台内部 id 当业务 uid：

```sql
ALTER TABLE users ADD COLUMN IF NOT EXISTS external_ref TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS users_external_ref ON users (tenant_id, external_ref)
  WHERE external_ref IS NOT NULL;
```

### 3.2 触发器改造（`src/ao/triggers.py`）

现状 `fire_due` → `_plan_slots` → 逐槽位 `_spawn`（建 1 个 run）。改为：

```
fire_due
└── 逐槽位
    ├── _expand_subjects(row) -> [{"user_id","external_ref"}...]   # 依据 fanout_mode
    ├── fanout_mode == 'none'  -> _spawn(row, slot)                # 完全保持现状
    └── 否则                    -> _spawn_batch(row, slot, subjects)
        ├── 先做重叠判定（按主体，见 3.3）
        ├── 逐主体 runs.create_run(...)，单个失败不回滚其他
        └── 落一条汇总 trigger_events
```

单主体 run 的构造：

```python
subject = {
    "trigger_id": row["id"],
    "scheduled_for": slot.isoformat(),
    "user_id": user["external_ref"] or user["id"],   # 业务主体，写进产物与计费
    "platform_user_id": user["id"],                   # 追溯用
}
window = f"{slot.isoformat()}#{subject['user_id']}"   # 幂等键带上主体
```

**幂等**：`runs.dedupe_key = sha256({workflow, version, subject, window})`，`window` 带主体后缀即可保证
「同一时刻、同一用户、重复投递」只落一个 run；同一时刻不同用户互不影响。事件触发（P2-T18）沿用
`dedupe_key_override`，扇出时改为 `f"{event_id}#{user_id}"`。

**审计**：一批只写一行 `trigger_events`，`payload` 存汇总：

```jsonc
{ "fanout_mode": "tenant_active", "total": 120, "created": 118,
  "skipped": 2, "failed": 0, "limit_hit": false, "run_ids_sample": ["run_...", "..."] }
```

`event_key = f"{trigger_id}:{int(slot.timestamp())}:fanout"`。**不要**一行一用户——1000 用户会把审计表打爆，
单用户结果查 `runs.subject->>'user_id'` 即可。

### 3.3 重叠策略按主体

现状 `_running_run(workflow_id)` 查该工作流任意在跑的 run，扇出后会导致「用户 A 卡住 → 用户 B~Z 全被 skip」。
改为 `_running_run(workflow_id, user_id)`，按 `subject->>'user_id'` 过滤。

策略支持矩阵：

| `overlap_policy` | `fanout_mode='none'` | 扇出 |
|---|---|---|
| `skip` | ✅ 现状 | ✅ 按主体 skip |
| `cancel_other` | ✅ 现状 | ✅ 只取消同一主体的旧 run |
| `buffer_one` | ✅ 现状 | ⛔ **不支持**：`pending_fire_at` 是单槽位，无法按主体缓冲。扇出时降级为 `skip` 并打一次 `warn` + 审计记 `detail="buffer_one_not_supported_with_fanout"` |

B 期若要 per-user 缓冲，再建 `trigger_pending_subjects` 表，本期不做。

### 3.4 预算与限流

三个数字要在一条计划里同时成立：

| 参数 | 位置 | 默认 | 语义 |
|---|---|---|---|
| `fanout_limit` | `triggers` | 1000 | 单次展开上限，超出**拒绝整批**（不做截断，避免"悄悄少跑一半用户"） |
| `budget_per_run` | `triggers` | 0（不限） | 透到 `runs.budget_usd`，网关侧硬限（P1-T12 已有） |
| `AO_FANOUT_BUDGET_CAP` | env | 0（不限） | 一批总预算上限 = `fanout_limit × budget_per_run` 的兜底闸门 |

超限行为：整批不建 run，落 `trigger_events.status='failed'`，`detail="fanout_limit_exceeded: 1200>1000"`，
同时 `logger.warning`。**不静默截断**——截断会让业务方以为全跑完了。

并发容量：扇出让 run 量从「1/时刻」变成「N/时刻」。`AO_EXEC_SLOTS` 默认 24，1000 用户 × 4 节点 ≈ 4000 次 turn，
需要在验收时实测（见 T08 的第 2 条），必要时调槽位或错峰（把 `scheduled_for` 打散到分钟级）。

### 3.5 费用维度

`usage.record()` 现在只拿 `run_id`。改为可传 `user_id`，由 `executor._turn` 从
`run.trigger_user_id` 传入（扇出时该值就是主体用户）：

```python
# usage.record(...) 增参 user_id: str | None
INSERT INTO llm_usage (run_id, node_id, attempt, purpose, model,
                       input_tokens, cached_tokens, output_tokens, cost,
                       gateway_request_id, user_id)
```

三层口径：

| 维度 | 查询 |
|---|---|
| 总额 | `SELECT SUM(cost) FROM llm_usage` |
| 工作流 | `JOIN runs ON runs.id = llm_usage.run_id GROUP BY runs.workflow_id` |
| 用户 | `GROUP BY user_id`（新索引，不再扫 JSONB） |

存量回填：`UPDATE llm_usage u SET user_id = r.trigger_user_id FROM runs r WHERE r.id = u.run_id AND u.user_id IS NULL`，
放在迁移里一次执行，量级为当前 `llm_usage` 行数（开发库 < 1 万，可接受）。

### 3.6 对象存储（跨机前置）

现状 `package.py` 只做本地内容寻址，`package_ref` 是本地路径——**多副本时 worker 看不到同一份包，
跨机 resume 也就无从谈起**（#10 #14）。

设计：

- 新增 `AO_PACKAGE_BACKEND`（`local` | `s3`，默认 `local` 保持零依赖）；
- `package.resolve_ref()` 对 `s3://bucket/key` 直接下载到 `settings.packages_dir/_cache/<manifest_hash>` 再本地加载，
  命中缓存不重复下载；
- 发布侧 `freeze_package()` 在 `s3` 模式下上传并写回 `package_ref = s3://...`；
- 依赖只引 `boto3`（或已有 S3 兼容网关的最小客户端），不进主链路默认路径。

**备选**（若暂不接对象存储）：多副本前先用共享卷（NFS / 云盘挂载）把 `packages_dir` 变成同一路径。
这只能算过渡——它把「多机」变成「共享存储单点」，跨可用区仍然不行。

### 3.7 代码改动表

| 文件 | 改动 |
|---|---|
| `migrations/012_fanout.sql` | 新建，见 3.1 |
| `src/ao/triggers.py` | `_expand_subjects` / `_spawn_batch`；`_running_run` 加 `user_id`；`buffer_one` 在扇出下降级 |
| `src/ao/runs.py` | `create_run` 增加 `subject` 透传已够用；新增 `list_runs_by_subject(workflow_id, user_id, window)` 供验收与排障 |
| `src/ao/scheduler.py` | 无需改（仍调 `fire_due`），但 `fire_due` 返回结构从「槽位列表」变成「槽位 + 扇出汇总」，日志要跟上 |
| `src/ao/manifest.py` | `Trigger` 增加 `fanout` 段校验（模式、spec 形状、limit 范围），发布期拒绝非法组合 |
| `src/ao/usage.py` | `record()` 增 `user_id`；新增 `summary_by_user(tenant_id, since)` |
| `src/ao/executor.py` | `_turn` 调 `usage.record` 时传 `run.trigger_user_id` |
| `src/ao/web/app.py` | 草稿页扇出配置（模式 + 名单 + 上限）；工作流详情页按用户展开最近 run；费用页加「按用户」 |
| `src/ao/package.py` | `resolve_ref` 支持 `s3://`，`freeze_package` 支持上传（B 期） |

## 4. 任务拆分与验收

| 任务 | 内容 | 验收 | 估时 |
|---|---|---|---|
| **T01** 用户目录接线 | `users.external_ref`；`list_active_users(tenant_id)`；`POST/GET /api/users`（仅运维）；测试数据 seed | 租户下 3 个 active 用户能被列出，`rejected` 的不出现 | 2d |
| **T02** 扇出数据模型 | `migrations/012`；`manifest.Trigger.fanout` 校验；`triggers.project` 落新字段 | 非法 `fanout_mode` / `limit=0` 在发布期被拒 | 1d |
| **T03** 展开与建 run | `_expand_subjects` + `_spawn_batch`；`window` 带主体；单主体失败隔离；汇总审计 | 10 用户 × 1 时刻 → 10 个 run，`subject.user_id` 各不相同；重复触发不新增 | 3d |
| **T04** 重叠按主体 | `_running_run(..., user_id)`；`buffer_one` 扇出降级 + 告警 | 用户 A 的 run 在跑时，B 照常触发；A 自己按策略 skip | 1.5d |
| **T05** 预算与限流 | `budget_per_run` → `runs.budget_usd`；`fanout_limit` 超限拒绝整批；`AO_FANOUT_BUDGET_CAP` | 1200 用户 / limit=1000 → 0 个 run + `failed` 审计 | 1.5d |
| **T06** 用户维度费用 | `llm_usage.user_id` + 回填 + 索引；`summary_by_user`；费用页 | 同一批里 A/B 的费用能分开查出，合计等于总额 | 2d |
| **T07** 对象存储 | `AO_PACKAGE_BACKEND=s3`；`resolve_ref` 下载缓存；发布上传 | 两进程分别从 s3 取同一 `package_ref` 都能跑通 | 3d |
| **T08** 端到端验收 | ①1000 用户扇出、②1 小时内有终态、③单用户重投幂等、④单用户卡住不阻塞他人、⑤批量费用可拆到用户 | 5 条脚本化，进 `scripts/drill.py` | 2d |

合计 15–17 人日。

## 5. 分期

**A 期（T01–T05 + T08，10 人日）**：扇出闭环，日程表主场景可跑。此时产物仍是数据（无前端）。
**B 期（T06–T07，5 人日）**：费用可拆用户、包可跨机，多副本才谈得上。

## 6. 风险与对策

| 风险 | 对策 |
|---|---|
| 1000 用户 × 4 节点 = 4000 次 turn，执行池被占满，白天对话被挤 | 创作池/执行池已物理隔离（P2-T06 决策）；扇出批次实测吞吐后调 `AO_EXEC_SLOTS` 或把 `scheduled_for` 打散到分钟 |
| 扇出后重叠判定变成 N 次查询 | `_running_run` 改为一次 `WHERE workflow_id=%s AND status IN ('queued','running') AND subject->>'user_id' = ANY(%s)`（与现有 478 行的判定范围保持一致），配合新索引 |
| 部分用户失败被"整批成功"掩盖 | 汇总审计记 `created/skipped/failed` 三个数；run 级失败仍走既有重试与 `paused_budget` |
| `buffer_one` 在扇出下语义不明 | 本期显式不支持并降级 + 告警，文档写清；要 per-user 缓冲需新表（B 期之后） |
| 对象存储引入新依赖与凭据 | 默认 `local` 保持零依赖；S3 只在 `AO_PACKAGE_BACKEND=s3` 时加载客户端；凭据走环境变量，不进包 |
| 用户名单来源不稳定（MCP 现查） | `tenant_active` / `static_list` 先落，`mcp_query` 放 B 期；MCP 失败时整批 `failed` 而不是跑半个名单 |

## 7. 本线不做

- 不做用户注册/登录/密码体系（`users` 表先当"引擎侧主体目录"，页面鉴权归 P3）。
- 不做 per-user 缓冲（`buffer_one` 扇出下不支持）。
- 不做扇出截断（超限整批拒绝）。
- 不做跨租户扇出（一个计划只展开本租户主体）。
- 不做前端产物（P4）。
- 不做 `graph.py` 子进程隔离（P1-T13）。
