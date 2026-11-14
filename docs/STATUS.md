# 实施状态

对照 `docs/superpowers/plans/2026-11-13-*.md`。图例：✅ 完成且测试覆盖；🟡 代码完成、待真实环境验证；⛔ 阻塞；⬜ 未开始。

## P1 执行内核

| 任务 | 状态 | 说明 |
|---|---|---|
| P1-T01 业内方案复核 | ✅ | `docs/research/2026-11-13-execution-kernel.md`（2026-11-14 检索 6 组关键词，来源 12 条）：确认 LangGraph 恢复粒度是 super-step，节点内崩溃整节点重跑；SKIP LOCKED 必须叠加租约 + fencing + 外部幂等键；LiteLLM key 六端点与 `delete` 为归档；`bwrap --unshare-all` 的 user/cgroup 是 "try"，容器内失败点通常在 mount。据此列出 6 项差异（`sandbox` 能力探测、`--unshare-all` 注释过强、缺外部幂等键、缺 key 吊销/预算、durability 未实证、无部分索引），1/2/5 归 P1-T13 前置，3/4 归 P1-T12 前置 |
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
| P1-T12 网关与记账 | 🟡 | 虚拟 key/spend log 适配器 + 去重；未连真实 LiteLLM 验证。**真跑时 `spent_usd=0`，因为本机没有网关，账单不可用**。转 ✅ 前需补：`/key/delete` 吊销、签发带 `max_budget`/`budget_duration`、部署要求共享 Redis（复核见研究文档 §5） |
| P1-T13~T17 沙箱 | 🟡 | 参数构造、输入中和、能力包、fail closed 已实现；bwrap 未在 Linux 实跑。2026-11-14 补 `sandbox.probe()`：实际起一次最小 bwrap 读 `uid_map` 断言 user namespace 真的生效（identity 映射判失败），带 60s 缓存；`executor` 真跑路径改为 `require_available()`（二进制 + 隔离双检），并按 bwrap(1) 修正 `--unshare-all` 的注释（user/cgroup 是 `-try`，不是强制）。单测 6 条，全量 110 passed |
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
| P2-T12 生成编排（七步） | ✅ | `src/ao/planner/orchestrator.py`：澄清→节点清单→包→graph→静态检查→冒烟→发布，每步落 artifact；e2e 覆盖成功发布与静态检查失败保留草稿两条路径。模型只在"节点清单"一步介入 |
| P2-T13 创作沙箱 | ⬜ | 依赖 P1-T13 的 bwrap，需 Linux 实跑 |
| P2-T14 生成失败修复循环 | ✅ | `orchestrator.py` 3-6 步内 3 轮回退（`AO_AUTHORING_REPAIR_ROUNDS`）：失败把 `code/message` 与上一版清单回喂模型重出节点清单，重刷提示词并清掉改名残留；artifact 带 `attempt`，成功发布时记录实际轮数。测试覆盖"第 2 轮修复后发布成功"与"3 轮用尽、草稿保留、不发布"两条路径 |
| P2-T15 对话页 UI（A 期） | ✅ | `src/ao/web/app.py` + 模板 + `migrations/004`：`prepare`（1-5 步，落草稿）/ `update_draft`（编辑四项并重建包）/ `publish_draft`（6-7 步）已拆开，`make run` 可用；真实 HTTP 验证"对话→草稿→改节点→发布"全链路 |
| P2-T16 MCP/Skill 工作流级绑定 | ✅ | `Manifest.bindings` + `migrations/005_resources.sql`（`resources` 表，global/private + owner）+ `ao/resources.py`（可见性、解析、发布期 `E_BINDING_MISSING/DISABLED/FORBIDDEN`）；`executor.py` 把绑定解析成 `mcp_servers` 注入 `RunEnv` 与 provider context（此前节点级字段是死字段）；生成器不再写节点级 `mcp/skills`，老包加载时并集提升；页面新增工作流级绑定面板，`make resources` 注册示例资源 |
| P2-T17 cron 定时触发 | ✅ | `migrations/006_triggers.sql`（投影表：`next_fire_at`、`owner_user_id`、`package_ref`，唯一索引保证一个工作流一条生效计划）+ `ao/cron.py`（5 段校验、按工作区时区算下次触发）+ `ao/triggers.py`（发布投影 / 到点扫描 / 幂等）；`scheduler.py` 在租约回收后 `fire_due`，CAS 推进 `next_fire_at` 抢占，重叠默认跳过（对齐 Temporal Skip），不补跑错过时刻；run 用 `scheduled_for` 当 `window` 复用 `runs.dedupe_key`，`trigger_events` 记 consumed/skipped/failed 审计；草稿页可填 cron，发布后显示下次触发。实测：发布→改表到点→`fire_due` 建 run→worker 跑通三节点 |
| P2-T18 事件型触发 + 错过时刻回填 | ✅ | `migrations/007`（`event_name`、`catchup_window_seconds`、`catchup_limit`，next_fire_at/cron 放开 NOT NULL）+ `migrations/008`（唯一索引放宽到 `(工作流,类型,cron,事件名)`，允许 cron 与 event 并存）；`triggers._plan_slots` 按"窗口 + 上限"算槽位，超限直接丢弃并把指针推到 now 之后（避免变成每轮速率）；`emit_event` + `POST /api/workflows/{id}/events/{name}`（`X-AO-Event-Id` 幂等、`X-AO-Event-Token` 鉴权，未配令牌仅 dev 放行）；幂等键支持 override，payload 差异不影响复用。实测：同事件重投复用同一 run；停机 20 分钟（每 5 分钟）补 1 个 + 重叠跳过 1 个 + 剩余 1 个放弃，next 推进到未来，worker 执行成功 |
| P2-T19 重叠策略扩展 + 手动 backfill | ✅ | `migrations/009`（`overlap_policy`、`pending_fire_at/pending_since`、审计加 buffered/cancelled）；`triggers._spawn` 按策略分派：`skip` / `buffer_one`（缓冲落库、满则丢弃、TTL 24h）/ `cancel_other`（`state.request_cancel` 置标志，节点边界退出，不强杀）；`_drain_pending` 在 `fire_due` 开头消费缓冲（CAS 清空，多副本安全）；`POST /api/workflows/{id}/triggers/{tid}/backfill`（区间 ≤31 天、槽位 ≤200、不带时区拒绝、忽略重叠、幂等键与正常触发一致、不改 `next_fire_at`）；草稿页新增策略下拉与缓冲展示、回填 curl 提示。不提供 terminate_other/allow_all（与"节点边界停止"和"无并发上限"冲突）。测试 8 条，全量 104 passed |

## P3 / P4

未开始。数据表 `share_links` / `share_access` / `bindings` / `trigger_events` 已建，供后续使用。
