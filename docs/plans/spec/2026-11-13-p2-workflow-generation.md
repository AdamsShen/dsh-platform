# P2 · 工作流生成（M2 / M3）

> **迁移说明**（2026-11-19 自 `auto-orchestration-platform/docs/superpowers/plans/` 迁入）
> 正文保留原文未改。文中 `docs/superpowers/{research,specs,experiments,decisions}/...`
> 形式的路径指向**原仓库**，未随迁；已落地文档在 `dsh-platform/docs/{research,reports}/`。
> 本文件是**事前规格**（要做什么、如何验收），实际执行结果与偏离记录见 `docs/STATUS.md`。

日期：2026-11-13
上游：`2026-11-13-p1-execution-kernel.md`（`wf_sdk` / `run_steps` / `load_package` 契约已冻结）
覆盖：M2 工作流包、M3 创作面
前置：P1-T03/T04 完成（SDK 与手写 `graph.py` 跑通）；P1-T06 完成（PG 可用）
估时：14–18 人日

## 1. 目标与退出条件

**目标**：非工程人员在一个对话页里贴需求文档，产出一条**可运行、可发布、可改提示词而不重生成代码**的工作流包。

退出条件：

1. 对话页从零产出一条 3 节点工作流并发布；包内文件齐全（`manifest.json` / `graph.py` / `prompts/*.md` / `prompts/*.system.md` / `ui/pages/*.json` / `tests/smoke.py`）。
2. 在 UI 上只改 `prompts/draft.md` 与节点绑定的 MCP → 发布 v2，`graph.py` 的 `mtime` 与 `hash` 不变，run 用 v2 跑通。
3. 手工把 `graph.py` 的节点集合改到与 manifest 不一致 → 发布被拒，错误码指明差异。
4. 生成代码里写 `import os` / 模块级发请求 / `eval()` → AST 检查拒绝，并给出具体行号。
5. 冒烟失败时把失败原因展示给人，最多 3 轮不通过则不发布（不产生 `workflow_versions` 行）。
6. 克隆一条工作流 → 新包、新 owner；改克隆体的提示词不影响原工作流。

## 2. 起点与终点

| 维度 | MVP 现状 | P2 终点 |
|---|---|---|
| 生成 | `generator.py` 关键词模板，不调模型 | DSH 创作会话产出 manifest + prompts + graph + page，七步可见可打断 |
| 包 | `workflow_versions.manifest_json` 一列 | 目录化包 + 内容寻址 + `package_ref` |
| 代码 | 平台解释器直接读 manifest | `graph.py` 由生成器产出，运行期加载冻结版本 |
| 校验 | manifest 结构校验 | AST 白名单 + 节点/边一致性 + 冒烟（含失败分支） |
| 提示词 | manifest 内联字符串 | `prompts/<node>.md` 与 `prompts/<node>.system.md` 两个独立文件 |
| 会话 | 无 | `planner_sessions` / `planner_messages` + 滚动总结 + 需求槽位 + 文档上传 |
| 复用 | 每次生成新工作流 | clone 派生 + `parent_id` 追溯 + user 级资源复制 |

## 3. 前提决策（开工前落 ADR）

| # | 决策 | 倾向 | ADR |
|---|---|---|---|
| D6 | `graph.py` 生成方式 | 骨架 + 声明式补丁（骨架固定，节点/边按 manifest 填） | `ADR-006-graph-codegen.md` |
| D7 | 包的存储 | 内容寻址：`packages/<workflow>/<version>-<manifest_hash>/`，PG 存 `package_ref` + `manifest_hash`；对象存储（minio/OSS）作为远端，本地留缓存 | `ADR-003-package-store.md`（与 P1 共用） |
| D8 | 冒烟 mock 策略 | 录制回放优先（`tests/fixtures/mcp/*.json`），缺录制用假实现；两者都在报告里标注 | `ADR-007-smoke-mock.md` |
| D9 | `action` 执行载体 | 受限 import 子进程 + bwrap（复用 P1-T13 启动器），不单独起服务 | `ADR-008-action-runtime.md` |
| D10 | manifest 版本兼容 | `manifest_version: 2`，平台同时读 v1（MVP 形状），读到 v1 时 warn 并自动升级 | `ADR-009-manifest-compat.md` |

## 4. 阶段 A：工作流包与发布（M2）

### P2-T01 业内方案复核

产出 `docs/superpowers/research/2026-11-13-workflow-generation.md`：

- LLM 生成代码的静态检查实践（AST 白名单 vs RestrictedPython vs 子进程隔离），重点看 LangGraph / CrewAI / AutoGen 的生成物校验方式。
- "清单文件是真值、代码是产物"的工程案例（如 Terraform plan/apply、K8s operator 的 CRD + controller 生成）。
- 生成代码一致性校验的常见做法（hash 校验、骨架版本号、节点集合对账）。
- 冒烟测试的 mock 录制回放方案（VCR 类工具在 LLM 工具调用场景的可用性）。

工具：`agent-reach`、`github-search`、`Deep Research`；结论附来源。

### P2-T02 manifest v2 与包结构

改动：`src/ao/manifest.py`（扩模型）、新增 `src/ao/package.py`。

manifest v2 增量字段（对齐设计 §7.2）：

- `manifest_version`、`tenant`、`limits{max_nodes,max_turns,max_loops,max_wall_seconds}`。
- 节点：`system` / `prompt` 改为**包内相对路径**；新增 `contract`、`capability`、`retry{max,backoff}`、`mcp` / `skills`（带 scope 前缀 `global.` / `user.`）。
- `ui.pages` / `ui.theme` 改为路径。
- `triggers`：`cron{timezone,fanout}` / `event` / `scan` / `api`。

包结构：

```
<workflow>@<version>/
  manifest.json
  graph.py
  prompts/<node>.md
  prompts/<node>.system.md
  contracts/<node>.json
  ui/pages/<page>.json
  ui/theme.json
  tests/smoke.py
```

验收：`tests/unit/test_manifest_v2.py`（v1 自动升级、缺文件报错、路径逃逸拒绝 `../`）。

### P2-T03 包存储与内容寻址

新增 `src/ao/store.py`（本地目录 + 对象存储适配）。

- 写包：先写临时目录 → `manifest_hash = sha256(canonical_json(manifest) + 逐文件 hash)` → rename 到 `<workflow>/<version>-<hash>/`（原子）。
- 读包：`load_package(ref)` 校验 hash，不一致拒绝加载（防止"跑的不是发布的"）。
- PG：`workflow_versions.package_ref` / `manifest_hash` / `smoke_ref` / `status`。

验收：`tests/integration/test_store.py`：写入后篡改 `graph.py` → 加载失败；同一 manifest 重复写入幂等。

### P2-T04 `graph.py` 生成器

新增 `src/ao/codegen.py` + `src/ao/codegen/skeleton.py`。

- 骨架固定（imports、`build_graph()`、`if __name__ == "__main__"`），只填节点函数与边。
- 每个节点函数体只有一行 `return await agent(...)` 或 `await action(...)`，不内联提示词（提示词由 SDK 按节点 id 读文件）。
- 生成后立刻做"反向解析"：`ast.parse` 抽出节点 id 集合与边集合，与 manifest 对账，不一致拒绝。

验收：同 manifest 生成两次字节一致（幂等）；节点 id 含 `-` 时生成合法的 Python 标识符映射。

### P2-T05 AST 白名单检查器

新增 `src/ao/static_check.py`，错误码：

| 码 | 规则 |
|---|---|
| `E_IMPORT` | 只允许 `wf_sdk`、`typing`、`json`、`datetime`、`re` |
| `E_SIDE_EFFECT` | 模块级不允许调用（除常量赋值、装饰器、`if TYPE_CHECKING`） |
| `E_EVAL` | 禁止 `eval` / `exec` / `compile` / `__import__` |
| `E_ATTR_ESCAPE` | 禁止 `__class__` / `__bases__` / `__subclasses__` / `__globals__` 穿透 |
| `E_GRAPH_MISMATCH` | 节点集合、边集合与 manifest 不一致 |
| `E_LOOP_UNBOUNDED` | 节点内 `while True` 或 `for` 无上界，或超过 `limits.max_loops` |
| `E_AGENT_WRITE` | `agent` 节点里出现 `action` 调用或写接口引用 |

验收：`tests/unit/test_static_check.py` 每码至少一例（正例 + 反例），错误信息带行号。

### P2-T06 冒烟执行器

新增 `src/ao/smoke.py`。

- mock MCP（录制回放）+ mock 模型（按 `outputs` 返回合法 ```result 块）。
- 必跑：全图 happy path + **一条失败分支**（`when` 为真与为假各跑一次）。
- 断言：节点全部有终态、`run_events` 有 `node.started` / `node.finished`、产物过契约、无越界写。
- 超时 `limits.max_wall_seconds`，超出判冒烟失败。

验收：`pytest tests/unit/test_smoke.py`；故意让某节点返回非法 JSON → 冒烟失败并给出节点 id。

### P2-T07 发布流水线

新增 `src/ao/publish.py`，串起 校验 → AST → 冒烟 → 冻结 → 落库。

- 任一失败：返回结构化错误（阶段、码、行号、原因），**不写 `workflow_versions`**。
- 通过：`status='published'`，写 `manifest_hash` / `package_ref` / `smoke_ref` / `published_at`，事件 `workflow.published`。
- 生成修复循环最多 3 轮（在 M3 的生成编排里），发布本身不重试。

验收：`tests/integration/test_publish.py` 四个失败分支 + 一个成功分支。

### P2-T08 版本冻结与运行期加载

改动：`src/ao/runner.py` / `executor.py`。

- 建 run 时写死 `version` 与 `package_ref`（已存在，补 `package_ref`）。
- 运行期只读冻结包；包被删/被改 → run `failed`（错误码 `package_missing`），不 fallback 到最新版。

验收：发布 v2 后重跑 v1 的 run → 仍用 v1 的包与提示词。

### P2-T09 绑定编辑（不发新代码）

新增路由 `/workflows/{id}/bindings` + 页面。

- 可改：节点用户词、系统词、`mcp` / `skills`、`vars` 声明、`limits`、`retry`、`timeout`、`capability`、页面 Schema。
- 不可改：节点增删、边增删（走"重新生成"流程）。
- 保存 = 生成新版本（`manifest_hash` 必变），并重跑**轻量冒烟**（不跑全图，只校验渲染与契约）。
- 系统词改动强制新版本并提示"影响该节点所有 run 的输出分布"。

验收：改词 → 发布 v2 → `graph.py` hash 不变；改节点结构 → 页面拒绝并指向生成流程。

### P2-T10 派生与克隆

新增 `POST /workflows/{id}/clone`。

- `workflow_defs.parent_id` 记录来源；新 owner = 当前用户。
- `global` 资源直接引用；`user` 级资源**复制成新 owner 的副本**（配置照抄，凭据需重新授权，缺凭据发布期告警）；`run` 级不复制。
- 克隆体独立改提示词/绑定，不回写原工作流。

验收：`tests/integration/test_clone.py`：克隆后改词不影响原工作流；`parent_id` 可追溯；缺凭据时发布告警。

## 5. 阶段 B：创作面（M3）

### P2-T11 会话、记忆与文档

新增 `src/ao/planner/session.py`、`memory.py`，表 `planner_sessions` / `planner_messages`。

- 一个对话页 = 一个 `planner_session`，绑定 `workflow_id`（可空，发布时回填）。
- 记忆：滚动总结（超过 N 轮压缩）+ 结构化需求槽位（主体、频率、产物、外部系统、约束）。
- 文档上传：解析成文本 + 附件引用（`uploads/<session>/<sha256>.<ext>`），大小与类型白名单。
- 崩溃可重开（不接管）；会话 token 计入 `platform.authoring`。

验收：`tests/integration/test_planner_memory.py`：20 轮后总结包含前 10 轮的槽位；上传 PDF/MD 后槽位能引用文件名。

### P2-T12 生成编排（七步）

新增 `src/ao/planner/orchestrator.py`，按设计 §7.4：

1. 澄清：agent 提问 → 落槽位（可跳过，明确写"按默认假设"）。
2. 节点清单：id / 标题 / 职责 / 输入输出 / 需要的 MCP 与 Skill。
3. manifest + prompts（Jinja2，变量显式声明）。
4. `graph.py` + Page Schema（P4 未就绪时先产出最小 Schema，字段留空但可校验）。
5. 静态检查（P2-T05）。
6. 冒烟（P2-T06）。
7. 发布（P2-T07）。

每一步产出落 `planner_messages`（`role='artifact'` + `payload` 指向包内文件），前端可按步查看与打断。

验收：`tests/e2e/test_generation.py`（mock 模型，确定性脚本）：走完七步；第 5 步注入非法 import → 回第 3 步并最终失败展示原因。

### P2-T13 创作沙箱

- 可写 `package/`，只读 `uploads/`，出网仅网关；能力包 `workspace_write`。
- 复用 P1-T13 启动器，配置不同（`AO_SANDBOX_PROFILE=authoring`）。
- agent 不得读其他 session 的 `uploads/`。

验收：`tests/e2e/test_authoring_sandbox.py`：写宿主路径失败；读别的 session 目录失败；`curl` 外网失败。

### P2-T14 生成失败的修复循环

- 第 5/6 步失败 → 带着结构化错误回第 3 步，最多 3 轮。
- 3 轮仍失败：不发布，把错误按"阶段/码/文件/行号"展示，并保留包草稿供人下载。
- 每轮失败原因落事件，供后续分析（不静默）。

验收：注入一个必失败的约束（如引用不存在的 `global.calendar`），三轮后页面显示明确原因且无 `workflow_versions` 行。

### P2-T15 对话页 UI 改造

改动：`src/ao/web/templates/chat.html` + 新模板。

- 左：对话与上传；中：需求槽位 + 节点清单；右：包文件树（点开看 prompts / manifest / graph）。
- 顶部步骤条（①–⑦），当前步高亮，失败步红色并显示原因。
- 发布按钮带版本号与 `manifest_hash` 前 8 位。

验收：手工走一遍完整流程；发布后跳转 `/workflows/{id}` 能看到 v1。

### P2-T16 复用与派生的端到端验收

- 克隆 → 改 3 处提示词 → 发布 v2 → 触发 → 与原工作流产物不同。
- 原工作流重新触发 → 产物不变（未被克隆体影响）。

验收：`tests/e2e/test_clone_flow.py`。

## 6. 与相邻线的接口

| 交给谁 | 内容 |
|---|---|
| P1 | 包加载走 `load_package(ref)`；运行期不读 DB 里的 manifest |
| P3 | 发布事件 `workflow.published` / `workflow.cloned`；版本与 `package_ref` 供成本报表聚合 |
| P4 | manifest v2 的 `ui.pages` / `ui.theme` 路径；Page Schema 校验器由 P4 提供，P2 只调用 |

## 7. 风险与对策

| 风险 | 对策 |
|---|---|
| 生成器产出与骨架版本漂移 | 骨架带 `SKELETON_VERSION`，写进 `graph.py` 头部；加载器校验版本，不匹配拒绝运行 |
| AST 白名单误报导致正常代码发不出去 | 错误码可查；误报先记录并加规则，**不放宽到黑名单** |
| 冒烟 mock 与真实 MCP 行为不一致 | 录制回放随包版本冻结；真实 MCP 变更触发冒烟重跑（P3 触发器里加订阅） |
| 对话页成本失控 | 每会话 token 上限 + `platform.authoring` 科目 + 超限提示用户新开会话 |
| 克隆时 user 级资源凭据复制不了 | 发布期告警并阻断，要求新 owner 授权；不静默指向原 owner |

## 8. 本线不做

- 节点结构的可视化编辑（增删节点仍靠重新生成）。
- Page Schema 的渲染与编辑（P4）。
- 工作流市场、跨租户模板共享。
- 生成物的人工代码编辑（`graph.py` 被视为产物，不提供编辑器）。
