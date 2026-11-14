# 实施状态

对照 `docs/superpowers/plans/2026-11-13-*.md`。图例：✅ 完成且测试覆盖；🟡 代码完成、待真实环境验证；⛔ 阻塞；⬜ 未开始。

## P1 执行内核

| 任务 | 状态 | 说明 |
|---|---|---|
| P1-T01 业内方案复核 | ✅ | `docs/research/2026-11-13-execution-kernel.md`（2026-11-14 检索 6 组关键词，来源 12 条）：确认 LangGraph 恢复粒度是 super-step，节点内崩溃整节点重跑；SKIP LOCKED 必须叠加租约 + fencing + 外部幂等键；LiteLLM key 六端点与 `delete` 为归档；`bwrap --unshare-all` 的 user/cgroup 是 "try"，容器内失败点通常在 mount。据此列出 6 项差异（`sandbox` 能力探测、`--unshare-all` 注释过强、缺外部幂等键、缺 key 吊销/预算、durability 未实证、无部分索引），1/2/5 归 P1-T13 前置，3/4 归 P1-T12 前置。差异 3（外部副作用幂等键）已落地：`migrations/010_effects.sql` 的 `run_effects` + `ao.effects.run_once`，键 `(run_id,node_id,action,params_hash)` 不含 attempt，`owner_epoch` 判断接管，`wf_sdk.action()` 经 `RunRuntime.action_once` 走它（2026-11-14 action 节点已移除，该调用路径不存在；`effects.run_once` 暂无调用方） |
| P1-T02 移植 DSH provider | ✅ | 2026-11-13 真实 key 验证：单轮 9.6s（含冷启动），示例工作流两节点 100% 成功；`scripts/probe_dsh.py` 可复跑 |
| P1-T03 wf_sdk | ✅ | `src/wf_sdk/`，含契约、repair、路径越界保护 |
| P1-T04 手写 graph + 加载器 | ✅ | `examples/daily_schedule/graph.py` + LangGraph 执行（mock 与真实 DSH 均跑通） |
| P1-T05 容量报告 | ✅ | `docs/reports/capacity-2026-11-13.md`：串行 p50 2.07s、4 路并发 p50 2.24s、成功率 7/7；未含 token 成本（无网关） |
| P1-T06 PG 迁移与全量表 | ✅ | `migrations/001_init.sql` + `002` |
| P1-T07 租约队列 + 心跳 + 回收 | ✅ | `queue.py`，测试覆盖独占领取/过期回收/续租 |
| P1-T08 状态机与失败分类 | ✅ | `state.py`，8 类分类与重试上限 |
| P1-T09 停止 | ✅ | 节点边界取消；queued 立即取消 |
| P1-T10 接管与 fencing | ✅ | epoch 校验 + 同机 resume + 过期回收 |
| P1-T11 变量解析 | ✅ | `vars.py`，run 级预取 + 节点懒解析 + `run_vars` 缓存；agent 兜底按设计 §19.1 未启用 |
| P1-T12 网关与记账 | 🟡 | 2026-11-14 用真实 LiteLLM 实例完成契约验证（`docs/reports/2026-11-14-gateway-probe.md`，探针 `scripts/probe_gateway.py` **14/14**）：`key_id` 改取 `token`（原取 `sk-...d4jw` 展示名）；`spend_logs` 改走 `/spend/logs/v2` 分页（`/spend/logs` 默认 `summarize=true` 是聚合，`summarize=false` 才有明细且截断 1 万行）；`normalize_spend` 从 `metadata.spend_logs_metadata` 取归因（请求头 `x-litellm-spend-logs-metadata` 可透传 run/node/attempt，**key 级 metadata 不进流水**，`metadata.tags` 会在同 key 上累积）。**2026-11-14 第二档接线完成（`AO_GATEWAY_ENABLED=1` 生效，默认关）**：①执行注入——`executor._turn` 每 turn 签虚拟 key（`max_budget` = run 剩余预算，网关侧硬限，超了 402 → `paused_budget`），`api_key`/`gateway_base_url` 经 `context` 透到 `dsh.py` → `_dsh_runtime` 覆盖 `base_kwargs`，turn 结束 `finally` 吊销；②归因——harness 只有 `base_url/api_key`、没有自定义 header 入口，所以放弃 `x-litellm-spend-logs-metadata`，改按 `SpendLogs.api_key` 反查新表 `gateway_keys`（`migrations/011`，token→run/node/attempt）；请求头归因仍是首选，缺失时才反查；③同步——`scheduler.sync_gateway_spend()` 按 `AO_GATEWAY_SYNC_INTERVAL`（默认 60s）拉「昨天到今天」，`sync_spend_logs` 按 `gateway_request_id` 去重并回刷 `runs.spent_usd`，跨天不漏；④失败策略——`AO_GATEWAY_FAIL_OPEN` 默认 0（fail closed，宁可不跑也不产生无归因账单），置 1 则直连并落 `gateway.degraded` 事件。`key_alias` 加 `uuid4` 后缀（实测别名删后仍占位，固定 alias 会让重试 400）。单测 `tests/unit/test_gateway_sync.py` 6 条（token 反查、header 优先、幂等、无归因丢弃、base_url 派生），全量 134 passed。**端到端实测（2026-11-14，真实 LiteLLM + 真实 DeepSeek 上游）**：DSH 真实调用 `ok`，同步 1 条，`llm_usage` 归因 `node_id=fetch / model=deepseek/deepseek-chat / cost=0.000206`。过程中钉住 4 个新事实并已修：①DSH 走 Anthropic `POST /v1/messages`，注入的 `base_url` 必须是根（拆成 `gateway_root()` / `gateway_openai_base()`）；②超预算实测返回 **422**（非 402，`type=budget_exceeded`），`_dsh_runtime._is_budget_error` 映射为 `budget_exceeded` → `paused_budget`，否则会进重试循环；③`/spend/logs/v2` 的 `end_date` 按当天 00:00 解析，同步窗口必须「昨天 → 明天」；④`mock_response` 模型不产 SSE，验 DSH 链路只能用真实上游。测试 8 条，全量 136 passed。**未做**：共享 Redis 多副本吊销、「被拒请求不写 SpendLogs」的对账语义。另：`key_alias` 全局唯一且删除后仍占位，重签须加唯一后缀；部署要求共享 Redis（多副本吊销/限流）；key/model 解析阶段就拒掉的请求不进 spend log，只能靠 `/gateway/daily/activity` |
| P1-T13~T17 沙箱 | 🟡 | 参数构造、输入中和、能力包、fail closed 已实现；bwrap 未在 Linux 实跑。2026-11-14 补 `sandbox.probe()`：实际起一次最小 bwrap 读 `uid_map` 断言 user namespace 真的生效（identity 映射判失败），带 60s 缓存；`executor` 真跑路径改为 `require_available()`（二进制 + 隔离双检），并按 bwrap(1) 修正 `--unshare-all` 的注释（user/cgroup 是 `-try`，不是强制）。单测 6 条，全量 110 passed。**2026-11-14 容器内实测**（Docker/OrbStack + bwrap 0.12.0）：默认 seccomp 报 `No permissions to create a new namespace`；放开 seccomp 后前移到 `Can't mount proc on /proc`；加 `CAP_SYS_ADMIN` 又前移到 `pivot_root`；只有 `--privileged` 或"放开 seccomp + 去掉 `--proc`/`--dev`"能跑通（uid_map `0 0 1`）。说明现有 `build_bwrap` 的 `--proc`/`--dev` 在非特权容器就是失败点，目标云容器仍需实跑。已把这两个挂载做成 `AO_SANDBOX_MOUNT_PROC` / `AO_SANDBOX_MOUNT_DEV`（默认 1，保持原行为），`SandboxSpec` 增加对应字段；沙箱单测 8 条，全量 112 passed |
| P1-T18 验收演练 | ✅ | `scripts/drill.py`：正常/契约失败/取数失败/停止/接管/预算六个场景 6/6（mock，确定性）；真实 provider 已在 T02 覆盖 |

## P2 工作流生成

| 任务 | 状态 |
|---|---|
| P2-T02 manifest v2 | ✅ 主要字段（limits/vars/retry/capability/prompts 路径） |
| P2-T03 包存储 | ✅ 本地内容寻址 + 原子写入（对象存储待接） |
| P2-T04 graph.py 生成 | ✅ 骨架 + 声明式补丁，幂等 |
| P2-T05 AST 白名单 | ✅ 7 类错误码，含节点/边一致性 |
| P2-T06 冒烟 | ✅ mock 全图执行；失败分支待补 |
| P2-T07 发布流水线 | ✅ 结构→manifest→文件→AST→冒烟→冻结→落仓库 |
| P2-T11 会话、记忆与文档 | ✅ | `src/ao/planner/`（`session.py` / `memory.py` / `documents.py`）+ 迁移 003；8 项测试覆盖 20 轮压缩留存前 10 轮槽位、MD/PDF 抽取、文件名穿越与超大文件拒绝、authoring 记账；调研见 `docs/research/2026-11-13-planner-memory.md` |
| P2-T12 生成编排（七步） | ✅ | `src/ao/planner/orchestrator.py`：澄清→节点清单→包→graph→静态检查→冒烟→发布，每步落 artifact；e2e 覆盖成功发布与静态检查失败保留草稿两条路径。模型只在"节点清单"一步介入。**2026-11-14 真实 DSH 首次跑通补漏**：此前 e2e 全绿是因为 mock 模型固定输出 `kind=agent`；真实模型会把"取数/渲染"判成 `action`，生成的 `graph.py` 是 `await action("<id>")`，而 `wf_sdk._ACTIONS` 为空 → 冒烟必报「未注册的 action」→ 任何带 action 节点的工作流都发不出去。修法：①`_nodes_prompt` 明确"平台无 action 实现，一律写 agent"；②`_normalize_plan` 兜底把 `action` 降级为 `agent`，原值留在 `kind_requested`，第 2 步 artifact 的 `downgraded` 列出被判降级的节点，并打 warning；③`_nodes_prompt` 强化时间表达约束。实测三轮同一句"每天早上 8 点…"：3/3 全 agent、cron 均为 `0 8 * * *`（修前分别是 action 爆炸、cron 给 9 点、cron 直接缺失），prepare 3.0-6.9s、publish 0.8s。新增 e2e `test_action_nodes_downgraded_to_agent` 锁住该路径。**权限档位与 kind 解耦**：原 `_build_manifest` 按 `kind` 派生 `capability`（action→workspace_write，其余→read_only），导致 agent 节点一律写不了文件；现改为默认 `workspace_write`，节点可用 `capability` 显式收紧到 `read_only`，`full_access` 等平台不开放的档位只告警回落（`NODE_CAPABILITIES` 白名单）。链路：manifest → `executor._turn` context → `dsh.py` context_pack → `_dsh_runtime` 映射成 DSH `read-only`/`workspace-write`。实测真实模型 4 节点全部 `workspace_write`、cron `0 8 * * *`、prepare 4.8s/publish 1.0s。**2026-11-14 彻底移除 action 节点**（确认架构前提：每个节点都是一次 harness turn，不存在"非 LLM 的确定性动作"这种节点类型）：删 `wf_sdk.action` / `register_action` / `_ACTIONS`、`RunRuntime.assert_action_allowed` / `action_once`、codegen 的 action 分支与 `kind="action"` 参数、static_check 的 `E_AGENT_WRITE` 规则；`Manifest.Node.kind` 降为 `str` 仅兼容历史包，生成器统一写 `agent`，模型偶发输出 `kind=action` 直接忽略。`ao/effects.py` 与 `run_effects` 表保留（`run_once` 暂无调用方，等 harness/MCP 外部副作用接入）。真实模型复跑：`graph.py` 只有 agent、无 action 调用，4 节点 `workspace_write`，cron `0 8 * * *`，publish 0.8s，全量 128 passed |
| P2-T13 创作沙箱 | ⬜ | 依赖 P1-T13 的 bwrap，需 Linux 实跑 |
| P2-T14 生成失败修复循环 | ✅ | `orchestrator.py` 3-6 步内 3 轮回退（`AO_AUTHORING_REPAIR_ROUNDS`）：失败把 `code/message` 与上一版清单回喂模型重出节点清单，重刷提示词并清掉改名残留；artifact 带 `attempt`，成功发布时记录实际轮数。测试覆盖"第 2 轮修复后发布成功"与"3 轮用尽、草稿保留、不发布"两条路径 |
| P2-T15 对话页 UI（A 期） | ✅ | `src/ao/web/app.py` + 模板 + `migrations/004`：`prepare`（1-5 步，落草稿）/ `update_draft`（编辑四项并重建包）/ `publish_draft`（6-7 步）已拆开，`make run` 可用；真实 HTTP 验证"对话→草稿→改节点→发布"全链路 |
| P2-T16 MCP/Skill 工作流级绑定 | ✅ | `Manifest.bindings` + `migrations/005_resources.sql`（`resources` 表，global/private + owner）+ `ao/resources.py`（可见性、解析、发布期 `E_BINDING_MISSING/DISABLED/FORBIDDEN`）；`executor.py` 把绑定解析成 `mcp_servers` 注入 `RunEnv` 与 provider context（此前节点级字段是死字段）；生成器不再写节点级 `mcp/skills`，老包加载时并集提升；页面新增工作流级绑定面板，`make resources` 注册示例资源 |
| P2-T17 cron 定时触发 | ✅ | `migrations/006_triggers.sql`（投影表：`next_fire_at`、`owner_user_id`、`package_ref`，唯一索引保证一个工作流一条生效计划）+ `ao/cron.py`（5 段校验、按工作区时区算下次触发）+ `ao/triggers.py`（发布投影 / 到点扫描 / 幂等）；`scheduler.py` 在租约回收后 `fire_due`，CAS 推进 `next_fire_at` 抢占，重叠默认跳过（对齐 Temporal Skip），不补跑错过时刻；run 用 `scheduled_for` 当 `window` 复用 `runs.dedupe_key`，`trigger_events` 记 consumed/skipped/failed 审计；草稿页可填 cron，发布后显示下次触发。实测：发布→改表到点→`fire_due` 建 run→worker 跑通三节点 |
| P2-T18 事件型触发 + 错过时刻回填 | ✅ | `migrations/007`（`event_name`、`catchup_window_seconds`、`catchup_limit`，next_fire_at/cron 放开 NOT NULL）+ `migrations/008`（唯一索引放宽到 `(工作流,类型,cron,事件名)`，允许 cron 与 event 并存）；`triggers._plan_slots` 按"窗口 + 上限"算槽位，超限直接丢弃并把指针推到 now 之后（避免变成每轮速率）；`emit_event` + `POST /api/workflows/{id}/events/{name}`（`X-AO-Event-Id` 幂等、`X-AO-Event-Token` 鉴权，未配令牌仅 dev 放行）；幂等键支持 override，payload 差异不影响复用。实测：同事件重投复用同一 run；停机 20 分钟（每 5 分钟）补 1 个 + 重叠跳过 1 个 + 剩余 1 个放弃，next 推进到未来，worker 执行成功 |
| P2-T19 重叠策略扩展 + 手动 backfill | ✅ | `migrations/009`（`overlap_policy`、`pending_fire_at/pending_since`、审计加 buffered/cancelled）；`triggers._spawn` 按策略分派：`skip` / `buffer_one`（缓冲落库、满则丢弃、TTL 24h）/ `cancel_other`（`state.request_cancel` 置标志，节点边界退出，不强杀）；`_drain_pending` 在 `fire_due` 开头消费缓冲（CAS 清空，多副本安全）；`POST /api/workflows/{id}/triggers/{tid}/backfill`（区间 ≤31 天、槽位 ≤200、不带时区拒绝、忽略重叠、幂等键与正常触发一致、不改 `next_fire_at`）；草稿页新增策略下拉与缓冲展示、回填 curl 提示。不提供 terminate_other/allow_all（与"节点边界停止"和"无并发上限"冲突）。测试 8 条，全量 104 passed |

## P3 / P4

未开始。数据表 `share_links` / `share_access` / `bindings` / `trigger_events` 已建，供后续使用。
