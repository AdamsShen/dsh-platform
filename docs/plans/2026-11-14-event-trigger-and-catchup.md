# P2-T18：事件型触发 + 错过时刻回填

## 需求背景

C 期（P2-T17）把 cron 计划投影成 `triggers` 表并接到调度循环，但有两个明显缺口：

1. **只有时间触发**：`triggers.type` 预留了 `event` 却没人写，外部系统（上游工作流完成、
   业务 webhook）无法按事件触发某个已发布工作流。
2. **错过的时刻直接丢弃**：`_fire` 只把 `next_fire_at` 推进到"下一个未来时刻"。调度器停机
   一小时，这一小时里该跑的次数全部静默消失——对"每日 9 点日报"这种低频计划是致命的。

## 业内调研

| 来源 | 关键做法 | 对本项目的启示 |
|---|---|---|
| [Quartz Misfire](https://www.quartz-scheduler.net/documentation/best-practices) / [Quartz.NET misfire 指令](https://cronradar.com/blog/quartz-net-misfire-instructions) | `misfireThreshold`（默认 60s）判定"迟到"，再由 misfire instruction 决定：`FIRE_ONCE_NOW` 补一次、`DO_NOTHING` 丢弃、`IGNORE` 继续按原计划；`@DisallowConcurrentExecution` 时天然只补一次 | 迟到要区分"小延迟"和"大批错过"；补跑次数必须可控 |
| [Temporal Schedules](https://docs.temporal.io/evaluate/development-production-features/schedules) / [中文文档](https://docs.temporal.org.cn/schedule) | `catchupWindow`（默认 1 年）限定只补窗口内的；`Backfill` 可手动补指定区间；回填时所有动作立即执行，但受 overlap policy（Skip/BufferOne/BufferAll）约束 | 回填 = 窗口 + 逐条 + 重叠策略，三者必须一起设计 |
| [K8s CronJob](https://kubi.live/blog/kubernetes-cronjob-not-running-diagnosis) | `startingDeadlineSeconds` 是截止窗口；**错过超过 100 次直接不调度**（TooManyMissedTimes） | 必须有次数上限，否则停机一周后启动会瞬间打爆队列 |
| [Airflow catchup](https://airflow.apache.org/docs/apache-airflow/stable/authoring-and-scheduling/catchup.html) | `catchup=True` 从 start_date 全量补，靠 `max_active_runs` 限并发 | 全量补必须有并发闸门；本项目已有"同工作流只允许一个 queued/running" |

共性模式：**catch-up 窗口 + 次数上限 + 逐条按序补 + 重叠/并发闸门 + 审计**。
Quartz 的 misfire 指令与 K8s 的 100 次上限，本质都是"别让补跑变成雪崩"。

## 技术设计

### 数据（`migrations/007_event_triggers.sql`）

```sql
ALTER TABLE triggers ALTER COLUMN next_fire_at DROP NOT NULL;   -- event 型没有下次时刻
ALTER TABLE triggers ADD COLUMN event_name TEXT;                -- event 型匹配的事件名
ALTER TABLE triggers ADD COLUMN catchup_window_seconds INTEGER NOT NULL DEFAULT 3600;
ALTER TABLE triggers ADD COLUMN catchup_limit INTEGER NOT NULL DEFAULT 1;
-- type 放开到 cron/event，并加两条一致性约束
CHECK (type IN ('cron','event'))
CHECK (type <> 'event' OR event_name IS NOT NULL)
CHECK (type <> 'cron'  OR next_fire_at IS NOT NULL)
CREATE INDEX triggers_event_lookup ON triggers (workflow_id, event_name) WHERE enabled;
```

默认 `catchup_window_seconds=3600` / `catchup_limit=1`：停机一小时内最多补 1 次。
选这个默认值而不是 Temporal 的"1 年"或 Airflow 的"全量"——日报类计划停机一天后补 48 次
没有业务意义，只会烧钱；需要更激进回填的工作流在页面上把窗口/上限调大即可。

### 触发语义

`fire_due`（`src/ao/triggers.py`）改为三步：

1. **算槽位** `_plan_slots`：从 `next_fire_at` 起按 cron 逐条推进，跳过早于
   `now - catchup_window` 的时刻（窗口外直接放弃，防卡死），收集最多 `catchup_limit` 个
   `<= now` 的时刻，同时算出 CAS 目标（最后一个槽位之后的下一个时刻）。
2. **CAS 抢占**：一次 `UPDATE ... WHERE next_fire_at = 旧值` 推进到目标时刻；影响 0 行说明
   别的调度器已处理。
3. **逐条建 run**：按时间顺序，每个槽位先查重叠（同工作流已有 `queued/running` 就记 `skipped`
   并结束本轮），否则 `create_run(window=槽位时刻)`；`runs.dedupe_key` 吸收重复投递。

槽位为空（全部在窗口外）也写一条 `skipped`（`detail=超出回填窗口`），否则运维看不出"为什么
没跑"。

### 事件触发

- `triggers.project` 支持 `type=event`：写 `event_name`、`next_fire_at=NULL`。
- `triggers.emit_event(workflow_id, event_name, payload, idempotency_key)`：
  找到启用的 event 计划 → `create_run(window=f"event:{key}")`，`subject` 带事件名与 payload。
  没有匹配计划时返回 `ignored`（webhook 语义：返回 2xx，不触发对端重试风暴）。
- 入站接口 `POST /api/workflows/{workflow_id}/events/{event_name}`：
  - `X-AO-Event-Id` 作为幂等键（缺省则每次新 run）；
  - `X-AO-Event-Token` 校验 `settings.event_token`；未配置时仅 dev 环境放行，生产直接拒绝
    （安全默认：宁可不收，不可裸收）。
- 事件触发**不跳过重叠**：事件是显式意图，排队即可；并发仍由"同工作流一个 queued/running"
  的既有约束兜底。

### 文件清单

| 文件 | 改动 |
|---|---|
| `migrations/007_event_triggers.sql` | 新增列、约束、索引 |
| `src/ao/triggers.py` | `_plan_slots` 回填、`project` 支持 event、`emit_event`、`_record` 支持自定义 key |
| `src/ao/manifest.py` | `Trigger` 加 `event_name` / `catchup_*`；校验 event 名与取值范围 |
| `src/ao/settings.py` | `event_token`（`AO_EVENT_TOKEN`） |
| `src/ao/web/app.py` | 事件入站 API；`/schedule` 收 catchup 参数 |
| `src/ao/planner/draft.py` + `orchestrator.py` | `apply_triggers` 透传 catchup |
| `src/ao/web/templates/planner.html` | 定时卡片加"回填窗口/最多补跑"，事件计划只读展示 |
| `tests/e2e/test_triggers.py` | 回填、窗口外丢弃、事件触发与幂等、API 令牌 |

## 备选方案

- **直接抄 Airflow 全量回填**：停机一周后一次补几百次，且每次都要花模型钱。放弃。
- **用独立的 `missed_runs` 表排队补跑**：多一张表和一次状态机，现有 `trigger_events` 已能
  记录每个槽位的 consumed/skipped，没必要。放弃。
- **事件触发走 MQ（Redis Stream / NATS）**：引入新基础设施，且 HTTP 入站已满足"上游系统
  触发"这一场景；真需要高吞吐再接。放弃。

## 状态

- [x] 方案设计（含调研）
- [x] 迁移 + 回填逻辑（007 列与约束、008 唯一索引放宽）
- [x] 事件投影与入站接口（含幂等 override 与令牌鉴权）
- [x] 页面与清单校验（回填参数表单、事件计划展示）
- [x] 测试与真实 HTTP 验证（13 条用例；事件重投同 run、停机 20 分钟回填、worker 执行成功）
