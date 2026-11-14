# C 期：定时触发（cron schedule）

## 需求背景

创作面已能"对话 → 草稿 → 发布"，但发布的版本只能手动跑。`Trigger` 模型里
`cron/timezone` 从 P1 就存在，`manifest.triggers` 却始终是硬编码的
`[{"type": "manual"}]`，`scheduler.py` 只做租约回收——**没有任何东西会到点创建 run**。

本期目标：

1. 发布时把 `manifest.triggers` 投影成可扫描的行，带下次触发时间；
2. 调度循环到点创建 run（tenant / user / package_ref 从投影行来）；
3. 同一时刻只触发一次（多实例、重启、重复扫描都不能重复）；
4. 版本切换时旧计划自动失效，不误触发老版本；
5. 创作面能看到/编辑 schedule，发布后显示下次触发时间。

## 业内调研

| 来源 | 关键做法 | 对本项目的启示 |
|---|---|---|
| [Temporal Schedules](https://docs.temporal.io/evaluate/development-production-features/schedules) | Schedule 是独立对象（有自己的 Id），与它启动的 Workflow 解耦；改计划不用重新部署代码 | 计划要投影成**独立表**，不能只躺在 manifest 里 |
| [Temporal Schedule 重叠策略](https://docs.temporal.org.cn/schedule) | 默认 `Skip`：到点时上次还在跑就跳过；另有 BufferOne/BufferAll/CancelOther；missed run 靠 backfill 补 | 先只实现 `Skip`，最省事也最不容易雪崩；backfill 明确不做 |
| [Temporal 文档示例](https://datasops.com/blog/temporal-workflow-engine) | workflow id 用 `{scheduled_time}` 拼，天然幂等 | 用 `scheduled_for` 当 `runs.window`，复用既有 `dedupe_key` |
| [分布式调度器设计](https://umamahesh.net/system-design-case-study-designing-a-distributed-job-scheduler) | leader 单活 + 每 job 存 `next_run_at`，到点用 CAS 更新抢占 | 已有 PG advisory lock，再加一层 `UPDATE ... WHERE next_fire_at = 旧值` 做 CAS |
| [Job scheduler 设计](https://techinterview.org/system-design-task-scheduling-system) | 调度器崩溃后靠 DB 里的下次触发时间恢复，不依赖内存 | 全部状态落表，进程重启后照样续跑 |

归纳出的通用模式：**投影表（next_fire_at）+ leader 单活 + CAS 抢占 + 幂等键 + 默认跳过重叠**。
本项目与之完全兼容（已有 advisory lock、`runs.dedupe_key`），不需要引入新基础设施。

## 技术设计

### 数据

`migrations/006_triggers.sql`

```sql
CREATE TABLE triggers (
  id, workflow_id, version, tenant_id, owner_user_id,
  type, cron, timezone, package_ref,
  enabled, next_fire_at, last_fired_at, created_at
);
CREATE UNIQUE INDEX triggers_active_per_workflow ON triggers (workflow_id) WHERE enabled;
CREATE INDEX triggers_due ON triggers (next_fire_at) WHERE enabled;
```

- 一个工作流同时只允许一条 `enabled` 计划（唯一索引保证，发布即切换）；
- `owner_user_id` 决定 run 的 `trigger_user_id`（触发身份，未来做租户配额要用）；
- `package_ref` 冗余存一份，触发时不用再回查版本表。

`trigger_events`（001 已有）复用为审计/幂等记录：`event_key = "<trigger_id>:<epoch>"`，
`status` 增加 `skipped`（重叠跳过）。

### 代码

| 文件 | 改动 |
|---|---|
| `src/ao/cron.py` | `validate_cron` / `next_fire`：只认 5 段表达式，时区用 `zoneinfo` |
| `src/ao/triggers.py` | `project`（发布投影）/ `fire_due`（扫描触发）/ `for_workflow`（页面展示） |
| `src/ao/scheduler.py` | `reap_once` 末尾调 `fire_due`（与租约回收同一循环、同一 advisory lock） |
| `src/ao/manifest.py` | `parse` 校验触发器：`type ∈ {manual,cron}`、cron 合法、时区存在 |
| `src/ao/planner/orchestrator.py` | 计划里的 `schedule` 落 `draft.triggers`；`_build_manifest` 写 triggers；发布成功后 `triggers.project` |
| `src/ao/planner/draft.py` | `Draft.triggers` + `apply_triggers`（页面覆盖式提交） |
| `src/ao/web/app.py` + 模板 | 草稿页 cron 输入；已发布页显示下次触发 |

### 触发语义

1. `fire_due` 取出 `next_fire_at <= now()` 的行；
2. CAS 抢占：`UPDATE triggers SET next_fire_at = <下一个未来时刻> WHERE id = ? AND next_fire_at = <取到的值>`，
   影响 0 行说明别的进程先处理了，直接跳过；
3. 重叠检查：同 workflow 已有 `queued/running` 的 run → 记 `skipped`，**不排队补跑**；
4. `create_run(window=scheduled_for)`：`dedupe_key` 天然吸收重复投递；
5. 写 `trigger_events` 审计（consumed / skipped / failed + run_id）。

**明确不做**：missed run 回填（调度器停了两天不会一次补 48 次）、`BufferOne/CancelOther`
重叠策略、事件型触发。都是后续按需加，表结构已预留 `type` 字段。

## 备选方案

- **APScheduler/Celery Beat**：进程内存里存计划，重启丢状态、多实例要额外协调；且我们要的
  只是"每 10 秒扫一次表"，引入调度框架反而多一层。放弃。
- **不改 manifest，直接扫 `runs` 的 subject 推下一次**：计划和运行纠缠，改 cron 要动历史 run。放弃。
- **每个 cron 起一个 asyncio 任务 sleep 到点**：进程重启全丢，且长睡眠在多数部署里不可靠。放弃。

## 状态

- [x] 方案设计（含调研）
- [x] 迁移 + cron/triggers 模块
- [x] 发布投影 + 调度接线
- [x] 创作面编辑与展示
- [x] 测试与真实 HTTP 验证（7 条新用例；发布 → 改表到点 → `fire_due` 建 run → worker 三节点 succeeded）
