# 实施状态

对照 `docs/research/`（调研）、`docs/plans/spec/`（P1~P4 事前规格，2026-11-19 自 `auto-orchestration-platform` 迁入）与 `docs/plans/`（P2 触发线的补充计划）。图例：✅ 完成且测试覆盖；🟡 代码完成、待真实环境验证；⛔ 阻塞；⬜ 未开始。

最后更新：2026-11-19（**P6 完成**：节点级提示词 / MCP / Skill 绑定落地，需求 #2 #3 转 ✅，见「P6 节点级提示词与绑定」一节。同日早前按 14 条需求逐条核对代码，修正"P1 / P2 全部收口"的失真结论：P1 沙箱组拆成 5 行、P2 补登 T01/T08/T09/T10 并做编号归一、文末新增需求覆盖总表与计划修订清单。2026-11-14 那条"全部收口"仅表示当时已实现部分跑通，不代表 14 条需求全覆盖）。

**编号口径（2026-11-19 起生效）**：规格编号（`docs/plans/spec/`）与 STATUS 历史编号曾出现撞车，以本文件结尾「编号映射」一节为唯一裁决。冲突点：规格的 `P2-T16` 是"复用与派生验收"，STATUS 历史里的 `P2-T16` 是"MCP/Skill 工作流级绑定"——后者保留原编号但语义只指绑定，前者改为 `P2-T20`。

## 执行进度（按 `docs/plans/2026-11-19-remaining-work-plan.md`）

| 里程碑 | 工作项 | 状态 | 验收（可复跑） |
|---|---|---|---|
| M0 | W01 提交工作区 | ✅ | 3 个提交已推送（`da9cc38`） |
| M0 | W02 补 ADR | ✅ | `docs/decisions/` 8 份（001/002/003/005/006/010/011/012） |
| M0 | W03 修正失效命令入口 | ✅ | `make demo` / `make drill` / `make drill-fanout` 可跑（原 `scripts/demo.py`、`scripts/capacity.py` 不存在） |
| M1 | W10 用户目录接线 | ✅ | `users.external_ref` + `src/ao/users.py` + `GET/POST /api/users` + `scripts/seed_users.py` |
| M1 | W11 扇出数据模型 | ✅ | `migrations/012_fanout.sql` + `manifest._validate_fanout` + `triggers.project` 落 4 列 |
| M1 | W12 展开与建 run | ✅ | `triggers._expand_subjects` / `_spawn_batch`，`window` 带主体，一批一条汇总审计 |
| M1 | W13 重叠按主体 | ✅ | `_running_run(workflow_id, user_id)`；`buffer_one` 扇出下降级 skip + warn |
| M1 | W14 预算与限流 | ✅ | `fanout_limit` 超限整批拒绝；`budget_per_run` → `runs.budget_usd`；`AO_FANOUT_BUDGET_CAP` |
| M1 | W15 端到端验收 | ✅ | `tests/e2e/test_fanout.py` 13 条 + `make drill-fanout` 5/5；全量 149 passed |

**M1 实测（2026-11-19）**：10 主体展开耗时 0.05s、重复投递 0.02s；单主体卡住只跳过 1 个（`created=9 skipped=1`）；1200>limit 整批 0 run + `failed` 审计。

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
| P1-T12 网关与记账 | ✅ | 2026-11-14 用真实 LiteLLM 实例完成契约验证（`docs/reports/2026-11-14-gateway-probe.md`，探针 `scripts/probe_gateway.py` **14/14**）：`key_id` 改取 `token`（原取 `sk-...d4jw` 展示名）；`spend_logs` 改走 `/spend/logs/v2` 分页（`/spend/logs` 默认 `summarize=true` 是聚合，`summarize=false` 才有明细且截断 1 万行）；`normalize_spend` 从 `metadata.spend_logs_metadata` 取归因（请求头 `x-litellm-spend-logs-metadata` 可透传 run/node/attempt，**key 级 metadata 不进流水**，`metadata.tags` 会在同 key 上累积）。**2026-11-14 第二档接线完成（`AO_GATEWAY_ENABLED=1` 生效，默认关）**：①执行注入——`executor._turn` 每 turn 签虚拟 key（`max_budget` = run 剩余预算，网关侧硬限，超了 402 → `paused_budget`），`api_key`/`gateway_base_url` 经 `context` 透到 `dsh.py` → `_dsh_runtime` 覆盖 `base_kwargs`，turn 结束 `finally` 吊销；②归因——harness 只有 `base_url/api_key`、没有自定义 header 入口，所以放弃 `x-litellm-spend-logs-metadata`，改按 `SpendLogs.api_key` 反查新表 `gateway_keys`（`migrations/011`，token→run/node/attempt）；请求头归因仍是首选，缺失时才反查；③同步——`scheduler.sync_gateway_spend()` 按 `AO_GATEWAY_SYNC_INTERVAL`（默认 60s）拉「昨天到今天」，`sync_spend_logs` 按 `gateway_request_id` 去重并回刷 `runs.spent_usd`，跨天不漏；④失败策略——`AO_GATEWAY_FAIL_OPEN` 默认 0（fail closed，宁可不跑也不产生无归因账单），置 1 则直连并落 `gateway.degraded` 事件。`key_alias` 加 `uuid4` 后缀（实测别名删后仍占位，固定 alias 会让重试 400）。单测 `tests/unit/test_gateway_sync.py` 6 条（token 反查、header 优先、幂等、无归因丢弃、base_url 派生），全量 134 passed。**端到端实测（2026-11-14，真实 LiteLLM + 真实 DeepSeek 上游）**：DSH 真实调用 `ok`，同步 1 条，`llm_usage` 归因 `node_id=fetch / model=deepseek/deepseek-chat / cost=0.000206`。过程中钉住 4 个新事实并已修：①DSH 走 Anthropic `POST /v1/messages`，注入的 `base_url` 必须是根（拆成 `gateway_root()` / `gateway_openai_base()`）；②超预算实测返回 **422**（非 402，`type=budget_exceeded`），`_dsh_runtime._is_budget_error` 映射为 `budget_exceeded` → `paused_budget`，否则会进重试循环；③`/spend/logs/v2` 的 `end_date` 按当天 00:00 解析，同步窗口必须「昨天 → 明天」；④`mock_response` 模型不产 SSE，验 DSH 链路只能用真实上游。测试 8 条，全量 136 passed。**遗留（部署期，不影响单实例运行）**：共享 Redis 多副本吊销与限流、key/model 解析阶段即被拒的请求不进 spend log（仅能靠 `/gateway/daily/activity` 对账）、`key_alias` 需唯一后缀（已用 uuid4）。
| P1-T13~T17 沙箱（合并记录，实际拆分与本行不同） | 🟡 | 明细见下方「P1 沙箱组拆分（2026-11-19 核对）」：T14 ✅、T15 ✅、T13 🟡（决策已反转）、T16 ❌、T17 🟡。原合并结论不再作为验收依据。以下为 2026-11-14 原始记录（保留作历史）：2026-11-14 决策：**不采用自研 bwrap/nsjail 层**，与 `sortie-deck` 对齐，执行隔离统一交给 harness 原生 permission mode。平台侧已有 `ao/harness/_dsh_runtime.py` 的 `_permission_mode()`（任务级 override > 全局配置 > `workspace-write`）、`_SANDBOX_TO_DSH` 映射，以及 sandbox 后端缺失时的探测与降级告警，无需第二套。**已删除**：`src/ao/sandbox.py` 全文（`SandboxSpec`、`build_bwrap`、`build_nsjail`、`launch_argv`、`readonly_roots`、`probe`/`require_available`、`resolve`/`CAPABILITIES`）、`executor.execute` 的 `require_available()` 守卫、`classify` 的 `SandboxUnavailable` 分支、`settings` 的 `sandbox`/`sandbox_profile`/`sandbox_mount_proc`/`sandbox_mount_dev`、`.env.example` 的 `AO_SANDBOX*`。**输入中和已接线**：迁至 `src/ao/input_guard.py` 并在 `executor._turn` 调用——harness 的 cwd 就是包根，会在那里自动加载 `AGENTS.md`/`CLAUDE.md`，而包由模型生成，不清理等于留下自我注入入口（模型写一个 AGENTS.md，下一轮就被当指令读入）。清理失败按 `InputGuardError` → `sandbox_denied`（在 `state.NO_RETRY` 内，不重试）。e2e `tests/e2e/test_turn_input_guard.py` 覆盖。**已知后果**：`graph.py` 仍由 `importlib.exec_module` 在 ao 主进程内加载，继承平台全部权限（env、PG 连接、文件系统、网络），当前无进程级隔离。容器与裸机实测结论保留在 `docs/reports/2026-11-14-sandbox-matrix.md`（非特权容器必须关 `/proc`、`/dev`；裸机不需要；`--unshare-all` 会断网）。若要隔离 LLM 生成的代码，需另立方案（子进程 + IPC）。**2026-11-14 DSH 自带沙箱实测（本机 macOS + seatbelt，SDK 0.2.1rc2）**：`scripts/probe_dsh_sandbox.py` 四组全过——workspace-write 下 bash 可执行、可写 cwd；写 `$HOME` 被拒（`touch: Operation not permitted` + `sandbox: file access denied`）；read-only 下写 cwd 被拒，且升级到 workspace-write 报 `requires approval, but no approval channel`。平台侧 `_run_probe` 返回 `available=True`，`dsh_sandbox_auto_fallback` 开启也不会误判提权。结论：DSH 自带沙箱在本机可用，平台侧无需第二套；Linux 容器形态仍未实测。 |
| P1-T18 验收演练 | ✅ | `scripts/drill.py`：正常/契约失败/取数失败/停止/接管/预算六个场景 6/6（mock，确定性）；真实 provider 已在 T02 覆盖 |

### P1 沙箱组拆分（2026-11-19 核对）

| 任务 | 状态 | 现状与依据 |
|---|---|---|
| T13 进程隔离（`graph.py`） | 🟡 **决策已反转** | 2026-11-14 的"交 DSH 原生"结论不成立：`graph.py` 由 `importlib.exec_module` 在 ao 主进程加载，**不在 DSH 会话内**，DSH 的 `permission_mode` 覆盖不到它。要隔离只能走子进程 + IPC。规格原案（`docs/plans/spec/2026-11-13-p1-execution-kernel.md:191`）仍然有效，需按原案排期 |
| T14 输入中和 | ✅ | `src/ao/input_guard.py`，`executor._turn` 调用；`tests/e2e/test_turn_input_guard.py` 覆盖 |
| T15 system 序言 + `system_injected` | ✅ | `executor.py` / `harness/dsh.py` 落 `meta` |
| T16 出网白名单 + 凭据代理 | ❌ **零实现** | 全仓 `egress` / `allowlist` / 凭据代理零命中；`graph.py` 可读全部 env（含上游 key） |
| T17 能力包 + MCP 只读契约 | 🟡 | `capability` 档位与 `NODE_CAPABILITIES` 白名单有；MCP 注册期只读契约校验无（`resources.py` 只做可见性解析） |

## P2 工作流生成

| 任务 | 状态 | 说明 |
|---|---|---|
| P2-T01 业内方案复核 | ✅ 补登（2026-11-19） | 原记录在 `auto-orchestration-platform/docs/superpowers/research/`，迁入后为 `docs/research/2026-11-13-planner-ui.md` 与 `docs/research/2026-11-13-planner-memory.md`。此前从表中缺失，非未做 |
| P2-T02 manifest v2 | ✅ | 主要字段（limits/vars/retry/capability/prompts 路径） |
| P2-T03 包存储 | ✅ | 本地内容寻址 + 原子写入（对象存储待接） |
| P2-T04 graph.py 生成 | ✅ | 骨架 + 声明式补丁，幂等 |
| P2-T05 AST 白名单 | ✅ | 7 类错误码，含节点/边一致性 |
| P2-T06 冒烟 | ✅ | mock 全图执行；失败分支待补 |
| P2-T07 发布流水线 | ✅ | 结构→manifest→文件→AST→冒烟→冻结→落仓库 |
| P2-T08 版本冻结与运行期加载 | ✅ 补登（2026-11-19） | 功能早已存在：`publish.freeze_package()` 冻结包并写 `manifest_hash`，`package.resolve_ref()` 支持 `<id>@<version>` / `<id>/<version>-<hash>` 运行期解析；此前未登记，非未做 |
| P2-T09 绑定编辑不发新代码 | ✅ 补登（2026-11-19） | `POST /planner/{session_id}/bindings` 改的是 `manifest.bindings` 与草稿，不触发代码重生成；此前未登记 |
| P2-T10 派生与克隆 | ❌ **未实现**（2026-11-19 核实） | `workflow_defs.parent_id` 只在建表与 `runs.ensure_workflow()` 出现，无 clone API、无"从已发布版本生成新草稿"入口 |
| P2-T11 会话、记忆与文档 | ✅ | `src/ao/planner/`（`session.py` / `memory.py` / `documents.py`）+ 迁移 003；8 项测试覆盖 20 轮压缩留存前 10 轮槽位、MD/PDF 抽取、文件名穿越与超大文件拒绝、authoring 记账；调研见 `docs/research/2026-11-13-planner-memory.md` |
| P2-T12 生成编排（七步） | ✅ | `src/ao/planner/orchestrator.py`：澄清→节点清单→包→graph→静态检查→冒烟→发布，每步落 artifact；e2e 覆盖成功发布与静态检查失败保留草稿两条路径。模型只在"节点清单"一步介入。**2026-11-14 真实 DSH 首次跑通补漏**：此前 e2e 全绿是因为 mock 模型固定输出 `kind=agent`；真实模型会把"取数/渲染"判成 `action`，生成的 `graph.py` 是 `await action("<id>")`，而 `wf_sdk._ACTIONS` 为空 → 冒烟必报「未注册的 action」→ 任何带 action 节点的工作流都发不出去。修法：①`_nodes_prompt` 明确"平台无 action 实现，一律写 agent"；②`_normalize_plan` 兜底把 `action` 降级为 `agent`，原值留在 `kind_requested`，第 2 步 artifact 的 `downgraded` 列出被判降级的节点，并打 warning；③`_nodes_prompt` 强化时间表达约束。实测三轮同一句"每天早上 8 点…"：3/3 全 agent、cron 均为 `0 8 * * *`（修前分别是 action 爆炸、cron 给 9 点、cron 直接缺失），prepare 3.0-6.9s、publish 0.8s。新增 e2e `test_action_nodes_downgraded_to_agent` 锁住该路径。**权限档位与 kind 解耦**：原 `_build_manifest` 按 `kind` 派生 `capability`（action→workspace_write，其余→read_only），导致 agent 节点一律写不了文件；现改为默认 `workspace_write`，节点可用 `capability` 显式收紧到 `read_only`，`full_access` 等平台不开放的档位只告警回落（`NODE_CAPABILITIES` 白名单）。链路：manifest → `executor._turn` context → `dsh.py` context_pack → `_dsh_runtime` 映射成 DSH `read-only`/`workspace-write`。实测真实模型 4 节点全部 `workspace_write`、cron `0 8 * * *`、prepare 4.8s/publish 1.0s。**2026-11-14 彻底移除 action 节点**（确认架构前提：每个节点都是一次 harness turn，不存在"非 LLM 的确定性动作"这种节点类型）：删 `wf_sdk.action` / `register_action` / `_ACTIONS`、`RunRuntime.assert_action_allowed` / `action_once`、codegen 的 action 分支与 `kind="action"` 参数、static_check 的 `E_AGENT_WRITE` 规则；`Manifest.Node.kind` 降为 `str` 仅兼容历史包，生成器统一写 `agent`，模型偶发输出 `kind=action` 直接忽略。`ao/effects.py` 与 `run_effects` 表保留（`run_once` 暂无调用方，等 harness/MCP 外部副作用接入）。真实模型复跑：`graph.py` 只有 agent、无 action 调用，4 节点 `workspace_write`，cron `0 8 * * *`，publish 0.8s，全量 128 passed |
| P2-T13 创作沙箱 | ✅ | 决策：不引入进程级沙箱，创作阶段风险用三层缓解——①冒烟强制 `settings.mock=True`（`smoke.py`），不联网不花钱，只验流程；②每次 harness turn 前清理包根的自动加载文件（`input_guard`，防模型自我注入）；③发布前 AST 白名单挡明显危险调用。剩余风险与正常运行一致：`graph.py` 由 `importlib.exec_module` 在主进程执行，继承平台权限。若将来要真隔离，需子进程 + IPC，环境约束见 `docs/reports/2026-11-14-sandbox-matrix.md` |
| P2-T14 生成失败修复循环 | ✅ | `orchestrator.py` 3-6 步内 3 轮回退（`AO_AUTHORING_REPAIR_ROUNDS`）：失败把 `code/message` 与上一版清单回喂模型重出节点清单，重刷提示词并清掉改名残留；artifact 带 `attempt`，成功发布时记录实际轮数。测试覆盖"第 2 轮修复后发布成功"与"3 轮用尽、草稿保留、不发布"两条路径 |
| P2-T15 对话页 UI（A 期） | ✅ | `src/ao/web/app.py` + 模板 + `migrations/004`：`prepare`（1-5 步，落草稿）/ `update_draft`（编辑四项并重建包）/ `publish_draft`（6-7 步）已拆开，`make run` 可用；真实 HTTP 验证"对话→草稿→改节点→发布"全链路 |
| P2-T16 MCP/Skill 工作流级绑定（编号语义只指本行的"绑定"，≠ 规格 T16） | ✅ | `Manifest.bindings` + `migrations/005_resources.sql`（`resources` 表，global/private + owner）+ `ao/resources.py`（可见性、解析、发布期 `E_BINDING_MISSING/DISABLED/FORBIDDEN`）；`executor.py` 把绑定解析成 `mcp_servers` 注入 `RunEnv` 与 provider context（此前节点级字段是死字段）；生成器不再写节点级 `mcp/skills`，老包加载时并集提升；页面新增工作流级绑定面板，`make resources` 注册示例资源。**2026-11-19 P6 修正**：这一行的"生成器不写节点级"已反转——节点级 ref 现在真正生效（`bindings_for_node()`），生成器可写、页面可勾选，工作流级只作为默认集 |
| P2-T17 cron 定时触发 | ✅ | `migrations/006_triggers.sql`（投影表：`next_fire_at`、`owner_user_id`、`package_ref`，唯一索引保证一个工作流一条生效计划）+ `ao/cron.py`（5 段校验、按工作区时区算下次触发）+ `ao/triggers.py`（发布投影 / 到点扫描 / 幂等）；`scheduler.py` 在租约回收后 `fire_due`，CAS 推进 `next_fire_at` 抢占，重叠默认跳过（对齐 Temporal Skip），不补跑错过时刻；run 用 `scheduled_for` 当 `window` 复用 `runs.dedupe_key`，`trigger_events` 记 consumed/skipped/failed 审计；草稿页可填 cron，发布后显示下次触发。实测：发布→改表到点→`fire_due` 建 run→worker 跑通三节点 |
| P2-T18 事件型触发 + 错过时刻回填 | ✅ | `migrations/007`（`event_name`、`catchup_window_seconds`、`catchup_limit`，next_fire_at/cron 放开 NOT NULL）+ `migrations/008`（唯一索引放宽到 `(工作流,类型,cron,事件名)`，允许 cron 与 event 并存）；`triggers._plan_slots` 按"窗口 + 上限"算槽位，超限直接丢弃并把指针推到 now 之后（避免变成每轮速率）；`emit_event` + `POST /api/workflows/{id}/events/{name}`（`X-AO-Event-Id` 幂等、`X-AO-Event-Token` 鉴权，未配令牌仅 dev 放行）；幂等键支持 override，payload 差异不影响复用。实测：同事件重投复用同一 run；停机 20 分钟（每 5 分钟）补 1 个 + 重叠跳过 1 个 + 剩余 1 个放弃，next 推进到未来，worker 执行成功 |
| P2-T19 重叠策略扩展 + 手动 backfill | ✅ | `migrations/009`（`overlap_policy`、`pending_fire_at/pending_since`、审计加 buffered/cancelled）；`triggers._spawn` 按策略分派：`skip` / `buffer_one`（缓冲落库、满则丢弃、TTL 24h）/ `cancel_other`（`state.request_cancel` 置标志，节点边界退出，不强杀）；`_drain_pending` 在 `fire_due` 开头消费缓冲（CAS 清空，多副本安全）；`POST /api/workflows/{id}/triggers/{tid}/backfill`（区间 ≤31 天、槽位 ≤200、不带时区拒绝、忽略重叠、幂等键与正常触发一致、不改 `next_fire_at`）；草稿页新增策略下拉与缓冲展示、回填 curl 提示。不提供 terminate_other/allow_all（与"节点边界停止"和"无并发上限"冲突）。测试 8 条，全量 104 passed |
| P2-T20 复用与派生端到端验收 | ⬜ 未开始（依赖 P2-T10） | 规格原文编号为 `P2-T16`，与上面"工作流级绑定"撞车，2026-11-19 起改为 `T20`。验收口径——克隆已发布工作流 → 克隆体独立改提示词与 MCP/Skill 绑定 → 发布新版本 → 原工作流版本与绑定完全不受影响 |

## P2 遗留清单

P2 功能面已交付 15 项（当前基线 `uv run pytest -q` → 136 passed, 1 deselected），以下为**不计入 ✅** 的遗留项，按影响排序。注意：**"P2 功能面收口"不等于 14 条需求收口**，需求口径以文末「需求覆盖总表」为准。

| # | 遗留项 | 现状 | 影响 | 何时必须处理 |
|---|---|---|---|---|
| 1 | **对象存储未接** | `package.py` 只做本地内容寻址：`compute_hashes` 对 manifest 规范序列化 + 逐文件 sha256 求出 `manifest_hash`，`freeze_package` 写回，`resolve_ref` 支持绝对目录 / `<id>@<version>` / `<id>/<version>-<hash>` 三种写法；包只落在 `settings.packages_dir` | 多机部署时各节点看不到同一份包，`package_ref` 是本地路径，无法跨机解析 | 上多副本 / 交付外部用户前 |
| 2 | **`graph.py` 无进程级隔离** | `executor._load_graph_module` 用 `importlib.util.spec_from_file_location` + `exec_module` 在 ao 主进程加载，顶层语句全部执行 | 继承平台全部权限：环境变量（含上游 key）、PG 连接池、整个文件系统、完整网络出口。LLM 生成的代码即平台权限 | 允许外部作者 / 不可信输入前；需子进程 + IPC，环境约束见 `docs/reports/2026-11-14-sandbox-matrix.md` |
| 3 | **manifest 只落"主要字段"** | 现有 `id` / `version` / `name` / `tenant` / `limits` / `vars` / `nodes` / `edges` / `triggers` / `bindings`，`Node` 有 `capability` / `retry` | 原设计中有字段未落，读外部生成的包可能静默丢信息 | 需要兼容非平台生成的包时 |
| 4 | **真实模型无自动化回归** | `tests/real/test_dsh_real.py` 默认 deselected（即全量输出里的 `1 deselected`）；P2-T12 / P2-T15 标注的"真实 DSH 跑通""真实 HTTP 验证"均为人工一次性验证 | CI 全绿 ≠ 真实链路通。换模型版本、改 prompt、改 cron 解析都可能静默破，e2e 用的 `mock_model` 覆盖不到 | 每次改提示词 / 换模型 / 动 `_dsh_runtime` 后人工复跑 |
| 5 | **P2 编号缺口无记录** | ✅ 已关闭（2026-11-19）：T01 / T08 / T09 核实为"做了未登记"，已补登为 ✅；T10 核实为**真未实现**，已在主表标 ❌ 并排入计划 | 原本无法判断是取消、合并还是遗漏，现已区分清楚 | 已处理 |
| 6 | **14 条需求中 2 条不满足**（#1 前端、#9 扇出） | 见下方「需求覆盖总表」。#3 节点级绑定与 #2 系统词可编辑已于 2026-11-19 由 P6 关闭（`bindings_for_node()` + 运行期按节点解析 + 节点卡片）；剩余两条不是补丁能解决：前端是独立子系统（P4 规格已写、未开工）、扇出需要触发器加一层展开 | 日程表主场景（每用户一份）不成立；工作流产物只能是数据不能是界面 | P5 扇出：立即；前端：等 #1 形态拍板 |

另有两处旧描述与实际不符，以此处为准（表格内容保留作历史）：

- **P2-T06** 原写"失败分支待补"，实际已覆盖：`tests/integration/test_publish.py::test_publish_fails_when_smoke_fails`，断言 `result.stage == "smoke"`。
- **各行末尾的 `104 / 128 / 134 / 136 passed`** 均为当时快照，非当前状态；当前为 **136 passed, 1 deselected**（P6 新增 10 条 `tests/e2e/test_node_bindings.py`）。

## P6 节点级提示词与绑定（2026-11-19 完成）

计划：`docs/plans/2026-11-19-node-bindings.md`。判定口径与实现落点：

| 任务 | 状态 | 实现 |
|---|---|---|
| T01 manifest 语义 | ✅ | `Manifest.bindings_for_node()`：工作流级为默认集，节点级 MCP 同名覆盖、Skill 并集去重；节点引用不带 scope 时沿用工作流级声明（避免私有绑定降级）。`NODE_BINDING_LIMIT = 8`，节点级 ref 走与工作流级相同的格式校验 |
| T02 运行期按节点解析 | ✅ | `RunRuntime._tools_for()` + `_tool_cache`（按节点缓存，重试/repair 不重复解析）；`binding.resolved` 每节点一条、`binding.skipped` 带 `node_id`；`ctx.mcp/ctx.skills` 在 `node_started` 校正为节点级结果 |
| T03 DSL 覆盖生效 | ✅ | `call_agent(mcp/skills=None)` → `_tools_for_turn()`：显式传参只覆盖当前 turn，不写缓存；`wf_sdk.agent()` 不再回落到 `ctx.mcp`（否则每 turn 都用工作流级默认值压掉节点绑定） |
| T04 发布期定位 | ✅ | `check_bindings()` 逐节点校验，issue 带 `node`；工作流级引用 `node=null`；节点级文案改为"在该节点绑定里重新选择" |
| T05 system 可编辑 | ✅ | `EDITABLE_FIELDS` 增加 `system/mcp/skills`；`prompts/<node>.system.md` 落用户版本（清空回默认），`manifest.json`/`graph.py` 字节不变 |
| T06 UI | ✅ | 节点卡片三件套（提示词 / 系统词 / MCP+Skill 勾选），选项来自 `list_available`；保存时 `update_draft` 即时校验节点 ref 存在性 |
| T07 端到端 | ✅ | `tests/e2e/test_node_bindings.py` 10 条：三节点（无绑定 / hr-db / hr-db+pdf-render）各 turn 的 `mcp_servers` 不同、`binding.resolved` 可审计、`graph.py` 仍是 `G = graph(...)` 并由 `build_state_graph` 编译、老包（节点级为空）合并结果不变 |

行为变化（有意为之）：节点级 `mcp/skills` 从"被丢弃"变为"生效"，包里写了不存在的 ref 会在保存/发布期报 `E_BINDING_MISSING` 并指到节点 —— `tests/e2e/test_generation.py` 里一条从未生效过的 `mcp__roles__get` 因此已删除。

## P3 / P4

两条线均未开工（P3-T03 触发器已提前做进 P2-T17~T19）。规格原文见 `docs/plans/spec/`，任务编号与验收标准以该目录为准。

| 线 | 规格 | 任务 | 估时 | 状态 |
|---|---|---|---|---|
| P3 控制面与大盘（M4 / M6） | `docs/plans/spec/2026-11-13-p3-control-plane.md` | P3-T01 ~ T10 | 12–16 人日 | ⬜ 仅 T03 完成（P2-T17~T19） |
| P4 前端生成器（M7 / M8 / M9） | `docs/plans/spec/2026-11-13-p4-page-generator.md` | P4-T01 ~ T11 | 16–20 → **18–24 人日** | ⬜ 未开始（2026-11-19 规格修订：产物改为自动生成的静态前端代码） |

数据表 `share_links` / `share_access` / `bindings` / `trigger_events` 已建，供后续使用。

## 需求覆盖总表（14 条，2026-11-19 核对）

需求原文见 `/Library/git-root/auto-orchestration-platform/docs/superpowers/specs/2026-11-13-harness-orchestration-platform-design.md` §0。本表按**代码实际**判定，不采信历史 STATUS 行。

| # | 需求 | 现状 | 依据（代码/文档） | 归属 |
|---|---|---|---|---|
| 1 | 对话页 + 传文档 + 记忆 + 出 LangGraph + **前端** | 🟡→❌ | 前三项齐（`src/ao/planner/`）；`ui/pages/main.json` 只写节点 ID 列表（`planner/orchestrator.py:345`），无 HTML/Schema/渲染管线 | P4（规格 2026-11-19 修订：产物改为**自动生成的前端代码**） |
| 2 | 节点提示词 + 系统词 UI 可绑 | ✅ | 2026-11-19 P6-T05：`EDITABLE_FIELDS` 含 `system`，节点卡片可编辑并落 `prompts/<node>.system.md`，清空回默认 | P6 已完成 |
| 3 | **节点级**绑定 MCP/Skill | ✅ | 2026-11-19 P6-T01~T04：`bindings_for_node()`（MCP 同名覆盖 / Skill 并集）+ `RunRuntime._tools_for()` 按节点解析并缓存；`call_agent` 的 `mcp/skills` 作为 turn 级覆盖真正生效；发布期 issue 带 `node` | P6 已完成 |
| 4 | 提示词支持 Jinja2 | ✅ | `SandboxedEnvironment + StrictUndefined`（`vars.py:23`） | 已完成 |
| 5 | harness 按变量查 MCP 取数 | 🟡 | 平台侧预取（`vars.py:87` `mcp.call`），不是 harness 驱动 | 口径差异，暂不阻塞 |
| 6 | 多人并发对话 / 多工作流并行 | ✅ | 2026-11-19 P5-T01：主体目录可读写（`src/ao/users.py` + `/api/users`），创作池/执行池已物理隔离；**鉴权仍是签名 cookie**（账号体系属 P3-T02，未开工） | P5-T01 已完成 |
| 7 | 生成与执行都在沙箱 | 🟡 | 执行在 DSH 沙箱；`graph.py` 仍 `importlib.exec_module` 在主进程 | P1-T13 |
| 8 | 工作流复用、可改提示词/MCP/Skill | 🟡 | 包可复用；`parent_id` 只建表，无 clone API | P2-T10 |
| 9 | 定时触发**按用户扇出** | ✅ | 2026-11-19 P5-T03~T05：`_spawn_batch` 一时刻展开 N 个 run，`subject.user_id` 与 `window` 带主体；重叠按主体判定；超限/超预算整批拒绝（`tests/e2e/test_fanout.py`） | P5-T03~T05 已完成 |
| 10 | 云上部署 | 🟡 | 单机可跑；包只落本地目录，跨机解析不了 | P5-T07 |
| 11 | 总/工作流/用户三层费用 | 🟡 | `llm_usage.user_id` 列与索引已随 012 落库，但 `usage.record` 尚未写入该列 | P5-T06 |
| 12 | 人工停止/重试/审计 | 🟡 | `state.request_cancel` / `state.resume` 有，13 个路由里无 `/stop` `/retry` | P3-T04 |
| 13 | 沙箱权限与提示词边界 | 🟡 | 输入中和 ✅（`input_guard.py`）；`egress` / `allowlist` / 凭据代理全仓零命中；MCP 只读契约无 | P1-T16 / T17 |
| 14 | 挂了自动接管 | 🟡 | 租约 + fencing + 同机 resume 有；跨机重放依赖对象存储 | P5-T07 + P1-T10 |

**统计（2026-11-19 M1 后）：✅ 5 条（#2 系统词、#3 节点级绑定、#4 Jinja2、#6 主体目录、#9 扇出）、🟡 8 条、❌ 1 条（#1 前端）。**

**结论：改计划，不重新制定。** 判据是地基是否还有效——14 条需求本身没变（设计 §0 就是这 14 行），租约队列 / fencing / 状态机 / 网关记账（真实 LiteLLM 14/14）/ 包冻结发布 / 规划七步均已实测在跑，重写等于把已验证的 12 人日再赌一次；3 条 ❌ 中有 2 条（前端、扇出）规格已存在，只是没排期。真正的问题是三处记录失真：沙箱组被一行 ✅ 打包、P2 编号撞车、P2 派生从未记录。

## 编号映射

| 出现位置 | 旧编号 | 现编号 | 语义 |
|---|---|---|---|
| 规格 `spec/2026-11-13-p2-workflow-generation.md` | P2-T16 | **P2-T20** | 复用与派生端到端验收 |
| 本文件 P2 表 | P2-T16 | P2-T16（不变） | MCP/Skill **工作流级**绑定，与规格 T16 无关 |
| 本文件 P2 表 | — | P2-T10 | 派生与克隆（原规格有、本表缺失） |
| P1 合并行 | P1-T13~T17 | 拆为 T13 / T14 / T15 / T16 / T17 | 五条独立验收 |

## 计划修订（2026-11-19 决策）

| # | 动作 | 对象 | 内容 | 估时 |
|---|---|---|---|---|
| 1 | **改写**（唯一推翻既有决策） | P1-T13 | "隔离交 DSH 原生"不成立（`graph.py` 不在 DSH 会话内），恢复规格原案"子进程 + IPC" | 5–7 人日 |
| 2 | **拆出重排** | P1-T16 / P1-T17 | 出网白名单 + 凭据代理（零实现）、能力包 + MCP 只读契约（半实现） | 3–4 人日 |
| 3 | **编号归一** | P2 | 规格 T16→T20；补登 T01/T08/T09；T10 标 ❌ 排期 | 1 人日（记录）+ 3–4 人日（T10/T20 实现） |
| 4 | **新增 P5** | `docs/plans/2026-11-19-multi-user-fanout.md` | 用户目录接线 + 触发器扇出 + 用户维度费用 + 对象存储，覆盖 #6 #9 #11 #10 #14 | 15–18 人日 |
| 5 | **补登** | P3 | stop / retry / audit 的 HTTP API（#12） | 2–3 人日 |
| 6 | **新增小线** | P6（节点级绑定 + system 可编辑） | ✅ **已完成（2026-11-19）**：节点级 MCP/Skill 覆盖工作流级、DSL `agent(mcp=)` 真正生效、`system` 进编辑面；实现与验收见「P6 节点级提示词与绑定」一节 | 5 人日 |
| 7 | **规格修订** | P4 | amis JSON 运行时 → **自动生成静态前端代码**（IR → 编译器 → `index.html`/`app.js`/`theme.css`），产物随 `workflow@version` 托管；T02/T05/T06/T07/T08/T10 不受影响 | 18–24 人日 |

**执行顺序（按依赖，不按需求编号）**：P5 扇出 → P3 运维 API → ~~节点级绑定~~（P6 已完成）→ 对象存储 → P4 前端 → P1-T13 隔离。

**已拍板（2026-11-19）**：需求 #1 的「前端代码自动生成」按**字面执行**——产出真代码，不是 JSON 配置。
落地方式取自洽解释，不是推翻安全边界：

- 页面真值仍是受控 Page Schema（= IR），**代码是编译产物**，不可手改；
- 编译器从 IR 生成可直接部署的静态前端（`index.html` + `app.js` + `theme.css` + `assets/`），随包冻结、按 `workflow@version` 托管；
- 反目标边界收紧为「禁止 LLM 直写前端源码、并以源码作为可编辑真值」——即禁止"源码即真值"，不禁止"代码由编译器生成"；
- 生成物档位：**A 档已确认**——无框架静态站点（生成的 DOM 构建函数 + 原生 JS + CSS 变量），零 Node 依赖，产物 < 100KB、可 diff、Python 直出；
- 连带影响：P4-T03 / T04 / T09 改法、估时 16–20 → 18–24 人日，见 `docs/plans/spec/2026-11-13-p4-page-generator.md` 文首「0. 修订」；
- 设计文档 §4 反目标第 2 条措辞需在 `auto-orchestration-platform` 仓库同步更新（尚未改，跨仓操作待确认）。

**已确认（2026-11-19，第二批）**：

1. **产物必须是 LangGraph**：现状已满足——生成物是 `wf_sdk` DSL（`G = graph(nodes=[...], edges=[...])`），`executor.build_state_graph()`（`src/ao/executor.py:549`）把它编译成真正的 `StateGraph`，`:677` 挂 `AsyncPostgresSaver` 做 PG checkpoint；`static_check` 的 `E_GRAPH_MISMATCH` + `_check_spec()` 两处保证无法绕过。本期不改代码，只把"不许绕过"写进 P6-T07 验收。
2. **每节点支持自定义提示词 + 独立 MCP/Skill**：计划见 `docs/plans/2026-11-19-node-bindings.md`（5 人日）。关键前提已核实：注入点是 per-turn 的（`executor.py:366`），所以节点级隔离不用重构会话，只换数据来源。
