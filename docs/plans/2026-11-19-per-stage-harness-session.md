# 每阶段一个 harness 会话（对齐 sortie-deck）

- 状态：已实现（2026-11-19）
- 日期：2026-11-19
- 影响：`executor.py`、`codegen.py`、`wf_sdk/`、`planner/`；**取代 M5 的 W50/W51 子进程方案**
- 参照：sortie-deck `docs/角色Agent设计问答与业界调研.md:240`

## 1. 需求（三条不变量）

1. **会话粒度**：一个工作流阶段 = 一个全新 harness 会话。不做"全任务一个会话"。
2. **resume 边界**：`native_id` 只在同一阶段内的多轮之间复用；跨阶段一律新建。
3. **跨阶段延续靠文件**：任务记忆、上游产物、决策结论都落文件，新会话开场从文件重建，不靠会话历史。

编排层（workflow）只负责"确定的事情"：节点顺序、并行、重试、状态推进。
执行层（节点内部怎么干）全部交给 harness 会话，平台只在阶段边界介入（校验产物）。

## 2. 业内对照

| 方案 | 做法 | 与本设计的关系 |
|---|---|---|
| Tarvos relay | 每段工作起新 agent 会话，读磁盘 plan 文件，交棒时写 40 行 Baton note | 与"新会话 + 文件交接"一致；可借鉴 Baton 的固定格式摘要 |
| Hermes / Kanban 式 | 一阶段一张卡、一个 fresh agent；board 是持久状态，卡是交接物 | 对应"节点行=状态，产物=交接物" |
| LangGraph checkpointer | 状态按 `thread_id` 保存，换 thread 即空白 | 印证「新会话=新 thread」，跨阶段记忆必须外置（store/文件） |
| sortie-deck | `start_session()` 每阶段一次；`resume` 仅阶段内多轮；`bootstrap_context()` 从 `memory.md`+产物索引+最近 10 轮重建 | **本项目直接对齐它** |

来源：
- https://agent-wars.com/insights/2026-03-13-tarvos-relay-architecture-for-building-large-projects-with-ai-coding-agents
- https://mer.vin/2026/04/managing-handoffs-in-multi-agent-coding-sessions-fresh-context-without-losing-continuity/
- https://docs.langchain.com/oss/python/langgraph/checkpointers

## 3. 现状与差距（代码定位）

已经成立的：

- 每个节点只发一次 harness turn：`src/ao/codegen.py:49-50`（注释即"每个节点都是一次 harness turn，没有第二种节点形态"）。
- 跨节点不继承会话：`src/ao/executor.py:230-241` 只在 `run_steps` 里查到**同一 node_id + attempt + 同机**的 `native_id` 才 resume，跨节点必然为 `None`。
- 阶段内已有一次 resume 场景：契约不过时 `_turn(repair=True)` 带 `native_id` 重发（`src/ao/executor.py:445`）。

差距：

| # | 差距 | 现状 | 目标 |
|---|---|---|---|
| G1 | 跨阶段交接不是文件 | `ctx.nodes` 里的上游 output 以变量注入模板，其余信息在会话外丢失 | 新会话开场读 `memory.md` + 产物索引 + 阶段摘要 |
| G2 | `graph.py` 仍是进程内可执行代码 | `src/ao/executor.py:695-703` 用 `exec_module` 在主进程加载 | 退化为声明式 DAG，执行体只有一种：harness turn |
| G3 | 阶段内多轮只有 repair 一种 | 只有契约重试能续会话 | 显式支持自审第 2..N 轮、人工升级后重入 |
| G4 | 并行节点共用 cwd | 所有节点 `cwd=package.root`（`executor.py:466` 附近） | 每节点独立可写目录，避免并行互踩 |

## 4. 设计

### 4.1 三层职责

```
编排层（确定性，LangGraph）
  节点清单 / 边 / 并行 superstep / 重试与 epoch fencing / 状态落库
        │  每个节点只做两件事：给输入包、收产物
        ▼
执行层（每节点一个 harness 会话）
  新会话 → 读 bootstrap 包 → 多轮执行（工具、MCP、skill）→ 产出 result 块 + 文件
        │  阶段内：同一 native_id 续轮；阶段结束：会话丢弃
        ▼
交接层（文件，唯一跨阶段通道）
  artifacts/           节点产物（权威）
  memory.md            滚动摘要：决策 / 结论 / 风险（append-only）
  context/             绑定上下文文件（只读）
  prompts/<node>.md    该阶段的 prompt 模板
```

会话是**可丢弃**的，记忆在文件里——这条要和 sortie-deck 的 `HarnessSession` docstring 保持同一口径。

### 4.2 阶段生命周期

```
node_started
  1. 解析本节点 bootstrap：subject + vars + 上游 output 指针 + artifacts 索引 + memory.md
  2. sanitize_inputs（已有，防自我注入）
  3. system = prompts/<node>.system.md（每次重新注入人格/契约，不依赖历史）
  4. native_id = 仅同 node+attempt 同机时可恢复，否则 None  → 新会话
  5. turn 1
  6. 需要续轮（repair / 自审 / 人工升级）→ 同一 native_id 再 send
node_finished
  7. 校验产物（coerce_outputs / parse_contract）
  8. 写阶段摘要（协调角色走 LLM 提炼，执行角色只记增量流水）
  9. 会话丢弃；native_id 仅留档，不再跨节点使用
```

### 4.3 `graph.py` 声明化（关键取舍）

当前 codegen 只会生成一种节点体（`await agent()` + `parse_contract`），也就是说"可执行 Python"带来的自由度是**零**，但风险是全量：生成代码能 import、能读 `os.environ`、能碰 PG。既然节点语义只有"一次 harness turn"，就没有理由让它保持可执行。

改为声明（推荐形态，二选一）：

```json
{"id": "draft", "title": "起草",
 "prompt": "prompts/draft.md", "system": "prompts/draft.system.md",
 "outputs": {"text": "str"}, "capability": "workspace_write",
 "turns": 1, "mcp": [], "skills": []}
```

- 执行体由平台唯一实现：读模板 → 开会话 → 多轮 → 校验。
- 父进程只解析数据，不 `exec_module` → **M5 的子进程 + IPC 不再需要**，安全边界从"进程隔离"降级为"代码不存在"。
- 若坚持保留 Python 形态，则 M5（子进程 + IPC）仍是必需的；两者**互斥**。

### 4.4 并行

- LangGraph 同一 superstep 的节点天然并行，每个节点各自开会话，会话层无冲突。
- 冲突在文件系统：并行节点当前共享 `package.root`。理想形态是 `workspace/<node_id>/` 可写、`artifacts/<node_id>/` 落产物、包根只读。
- 并行节点的上游指针在开始时快照，结束后再合并，避免读到半成品。

**2026-11-19 结论（G4 暂不做硬隔离）**：DSH 的权限模型只有三档
（`read-only` / `workspace-write` / `danger-full-access`），可写范围由 `cwd` 决定，
没有"额外可写目录"参数——见 `src/ao/harness/_dsh_runtime.py:93` 的映射与
`:781` 的档位落地点。要实现"包根只读 + 节点目录可写"只有两条路：

1. `cwd` 下沉到 `workspace/<node_id>/`，再把节点产物由平台回收进 `artifacts/`；
   代价是 `prompts/*.md` 里的 `artifacts/xxx` 相对路径全部失效，现有包与生成器都要改。
2. 每个节点一个独立沙箱根（复制只读包 + 单独可写目录）；代价是每节点一份包拷贝。

当前生成器只产串行链（`when` 分支也是单路径），并行踩踏的概率低于改造成本，
因此本轮不做硬隔离，只保留约定：并行节点产物写 `artifacts/<node_id>/...`。
若后续开放真正的并行扇出，再按路线 1 实施，并在验收里补"写包根失败"的用例。

## 5. 文件级改动清单

| 文件 | 改动 |
|---|---|
| `src/ao/codegen.py` | 节点体从 Python 生成改为输出声明 JSON（或保留 `graph.py` 但只允许字面量） |
| `src/ao/executor.py` | `_load_graph_module` 改为 `load_graph_decl`；节点执行收敛到唯一 `_run_stage`（bootstrap → send → 校验） |
| `src/ao/wf_sdk/` | 去掉 `agent()/parse_contract()` 的调用面，只留 `NodeSpec` 数据模型与校验 |
| `src/ao/memory.py`（新） | `record_turn` / `append_stage_summary` / `bootstrap_pack` |
| `src/ao/harness/` | `send` 增加 `turns` 语义：阶段内多次调用复用 `native_id` |
| `prompts/` | 每节点 `system.md` 由平台注入，每次会话重建 |
| `tests/` | `test_stage_session.py`（新会话隔离）、`test_stage_bootstrap.py`（文件交接）、并行隔离用例 |

## 6. 验收

1. 两个相邻节点：`run_steps` 里两条 `native_id` 不相等；第二阶段 `prompt` 里不含第一阶段对话历史，但含其 `artifacts` 索引与 `memory.md` 摘要。
2. 同一节点 repair 进入第 2 轮：`native_id` 与前一轮相同。
3. ~~并行两节点：各自 `workspace/<node>/` 可写，写包根失败。~~ 暂缓，原因见 4.4（受 DSH 三档权限模型限制）。
4. 删除 `graph.py` 中的可执行代码后，工作流仍能跑（证明执行体已完全收敛）。
5. 跨机接管：`native_id` 不可用 → 重跑当前阶段而不是续会话（沿用现有语义）。

## 7. 待确认

1. `graph.py` 是否退化为纯声明？（决定 M5 子进程方案是否直接取消）
2. 阶段摘要用 LLM 提炼还是纯确定性索引？（LLM 更省 token 但引入额外调用成本）
3. 阶段内多轮是否本期就要（自审 / 人工升级），还是只保留 repair？
