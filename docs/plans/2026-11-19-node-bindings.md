# P6：节点级提示词与 MCP/Skill 绑定

日期：2026-11-19
前置：P2-T02（manifest v2）、P2-T16（工作流级绑定，已完成）
覆盖需求：#2（节点提示词 + 系统词可绑）、#3（节点级 MCP/Skill）
估时：4–5 人日

状态：✅ **已完成（2026-11-19）**，T01~T07 全部落地。验收 `tests/e2e/test_node_bindings.py`（10 条），全量 `uv run pytest -q` 136 passed。实现记录见文末 §7。

## 1. 需求

1. **生成的工作流必须是 LangGraph**；
2. 每个节点支持**自定义提示词**（含 system 序言）；
3. 每个节点支持**独立的 MCP / Skill 绑定**，不是整个工作流共用一套。

### 1.1 LangGraph 不变式（已满足，本期不改）

生成物不是手写 `StateGraph`，而是平台 DSL：

```python
from wf_sdk import agent, graph, node, parse_contract

@node(id="draft", outputs={"plan_path": str}, title="生成日程")
async def draft(ctx):
    text = await agent()
    return parse_contract(text, ctx.contract)

G = graph(nodes=[draft, finalize], edges=[{"from": "draft", "to": "finalize"}])
```

`wf_sdk.graph()` 返回 `GraphSpec`，执行器 `build_state_graph()` 把它**编译成真正的 LangGraph `StateGraph`**
（`src/ao/executor.py:549` 的 `from langgraph.graph import END, START, StateGraph`，`:677` 挂 `AsyncPostgresSaver` 做 PG checkpoint）。

三道闸保证"不可能不是 LangGraph"：

| 闸门 | 位置 | 作用 |
|---|---|---|
| 静态检查 | `static_check` 的 `E_GRAPH_MISMATCH` | 必须有 `G = graph(nodes=[...], edges=[...])`，节点/边要与 manifest 一致 |
| 运行前校验 | `executor._check_spec()` | `GraphSpec` 与 manifest 对不上直接拒绝 |
| 编译入口 | `executor.build_state_graph()` | 只接受 `GraphSpec`，不接受任意 callable 图 |

结论：这一条**不需要新做**，只需要把"不许绕过"写进验收项（见 T07）。

## 2. 现状差距（按代码判定）

| 差距 | 位置 | 实际行为 |
|---|---|---|
| 节点级字段存在 | `manifest.py` `Node.mcp` / `Node.skills` | 有字段，但只为兼容历史包保留 |
| 节点级被合并成全局 | `manifest.py` `effective_bindings()` | 把所有节点的 `mcp/skills` **并集**进工作流级，`scope` 一律按 `global` |
| 运行期只解析一次 | `executor.py:99`（`RunRuntime.__init__`） | `resolve_bindings(manifest.effective_bindings())` 一次，全 run 所有节点共享 |
| DSL 参数被丢弃 | `executor.py:330-337` `call_agent(ctx, prompt, vars, mcp, skills)` | 形参收了 `mcp` / `skills`，函数体直接 `return await self._turn(...)`，**两个参数没用上** |
| 发布期报错不定位节点 | `resources.check_bindings()` | 遍历 `effective_bindings()`，issue 无 `node` 字段；文案写"先在工作流设置里选择"，节点级绑定时是错误指引 |
| 系统词不可编辑 | `planner/draft.py:22` `EDITABLE_FIELDS` | `("title","duty","outputs","prompt")`，`system` 生成成文件但不进编辑面 |

**好消息**：注入点是 **per-turn** 的——`_turn` 每次构造 `context` 时才带 `mcp_servers` / `skills`
（`executor.py:366-367`），`dsh.py:88` 从 context 读。所以节点级隔离不用重构会话，只换数据来源。

## 3. 设计

### 3.1 合并语义

| 来源 | 作用范围 | 合并规则 |
|---|---|---|
| `manifest.bindings`（工作流级） | 所有节点的默认工具集 | 基础层 |
| `nodes[].mcp` / `nodes[].skills`（节点级） | 仅该节点 | **MCP 按 server 名覆盖**（节点同名配置优先）；**Skill 并集去重**；节点未写 = 完全继承工作流级 |

不做"节点级排除工作流级"的语法（`!ref` 之类）——要排除就只在工作流级留公共部分，节点各写各的。

### 3.2 数据：不加表、不改 manifest 版本

字段已经在 `Node` 上，`manifest_version` 保持 2。新增一个纯函数：

```python
def bindings_for_node(self, node: Node) -> list[Binding]:
    """工作流级 + 该节点级；MCP 同名由节点级覆盖，Skill 去重。"""
```

`effective_bindings()` 保留（发布期汇总校验、老包兼容），但运行期不再用它。

### 3.3 运行期：按节点解析 + 缓存

```python
# RunRuntime
self._tool_cache: dict[str, tuple[dict, list[str]]] = {}

def _tools_for(self, node: Node) -> tuple[dict[str, Any], list[str]]:
    if node.id not in self._tool_cache:
        servers, skills, skipped = resources.resolve_bindings(
            self.manifest.bindings_for_node(node), user_id=self.user_id
        )
        if skipped:
            self._emit("binding.skipped", node_id=node.id, items=skipped)
        self._tool_cache[node.id] = (servers, skills)
    return self._tool_cache[node.id]
```

`_turn` 内：

```python
servers, skills = self._tools_for(node)
context={... "mcp_servers": servers, "skills": skills ...}
```

`call_agent` 把 DSL 传入的 `mcp` / `skills` 作为**节点内的临时覆盖**：
显式传了就覆盖该节点的绑定，没传就用节点解析结果——这是 `agent(mcp=[...])` 现在被丢弃的地方，改完才真正生效。

缓存粒度 = 节点：一个节点跑多个 turn（重试、repair）只解析一次；不同节点互不影响。

### 3.4 发布期：报错定位到节点

`check_bindings` 改为逐节点遍历 `bindings_for_node`，issue 增加 `node` 字段：

```jsonc
{"code": "E_BINDING_MISSING", "node": "fetch_users",
 "message": "节点 fetch_users 的 mcp 绑定不存在: hr-db（在该节点绑定里重新选择）"}
```

工作流级引用的 issue `node` 为 `null`，文案保持原样。错误码不变，避免破坏现有测试与前端。

### 3.5 提示词：system 进编辑面

- `EDITABLE_FIELDS` 增加 `system`；
- 草稿保存时写 `prompts/<node>.system.md`（与 `prompts/<node>.md` 成对），`manifest.nodes[].system` 记录相对路径；
- 校验：`system` 只做长度上限（4000 字）与 UTF-8 检查，不做内容审查（内容安全由输入中和与执行期兜）；
- 生成器第 ② 步的"节点清单"里 `system` 从"自动生成"变为"可产出、可被用户覆盖"，覆盖后重编译不改写它。

### 3.6 UI：节点卡片三件套

草稿页每个节点一张卡片：

```
┌ fetch_users ──────────────── capability: workspace_write ┐
│ 提示词  [多行文本，Jinja2 变量高亮]                        │
│ 系统词  [多行文本]                                        │
│ MCP     [x] hr-db  [ ] crm-api  [ ] report-db            │
│ Skill   [x] schedule-fmt  [ ] pdf-render                 │
└──────────────────────────────────────────────────────────┘
```

选项来源 `resources.list_available(user_id)`；工作流级绑定在页首作为"默认"展示，节点级勾选表示覆盖/追加。
节点级选了工作流级没有的资源，保存时即时校验（不等发布）。

### 3.7 生成器

`_nodes_prompt` 允许每个节点输出 `mcp` / `skills`，但**只能引用当前已注册的资源 ref**（提示词里注入候选清单），
未知 ref 由发布期 `E_BINDING_MISSING` 拦下并指到节点。不让模型臆造资源名。

## 4. 任务与验收

| 任务 | 内容 | 验收 | 估时 |
|---|---|---|---|
| **T01** manifest 语义 | `bindings_for_node()`；MCP 覆盖 + Skill 并集；老包行为不变 | 单测：节点级同名 MCP 覆盖工作流级配置；未写节点级 = 完全继承 | 0.5d |
| **T02** 运行期按节点解析 | `_tool_cache` + `_turn` 换数据源；`binding.skipped` 带 node_id | 两节点绑不同 MCP，`run_events` 里两次 turn 的 `mcp_servers` 不同 | 1d |
| **T03** DSL 覆盖生效 | `call_agent` 透传 `mcp` / `skills` | `agent(mcp=["x"])` 只影响该 turn；不影响同 run 其他节点 | 0.5d |
| **T04** 发布期定位 | `check_bindings` 逐节点 + `node` 字段；文案改节点级 | 节点绑了不存在资源 → 发布失败且错误指向该节点 | 0.5d |
| **T05** system 可编辑 | `EDITABLE_FIELDS` + `prompts/<node>.system.md` 落盘 | 改系统词 → 保存 → 重跑，事件里 `system` 变化；`manifest.json`/`graph.py` hash 不变 | 1d |
| **T06** UI | 节点卡片三件套 + 即时校验 | 勾选/取消 MCP、改两段提示词，保存后预览生效 | 1d |
| **T07** 端到端 | 三节点：A 无绑定、B 绑 hr-db、C 绑 hr-db+pdf-render；全程 LangGraph 执行 | ① 各节点工具集正确 ② `graph.py` 仍是 `G = graph(...)` 形态且由 `build_state_graph` 编译 ③ 老包（节点级字段为空）行为与改造前一致 | 0.5d |

合计 5 人日（与 STATUS 计划修订第 6 行的 4–5 人日一致）。

## 5. 风险与对策

| 风险 | 对策 |
|---|---|
| 每节点不同 MCP → 每 turn 重复建连，开销上升 | provider 侧按 server 名复用连接（已有 spec 缓存）；单节点绑定上限 8 个，超过发布期拒绝 |
| 私有资源 owner 变化导致运行期解析不到 | 发布期校验 + 运行期 `binding.skipped` 事件留痕；节点级 skipped 必须带 `node_id` |
| 老包节点级字段语义从"并集提升"改成"节点专属"，行为可能变化 | 老包（`nodes[].mcp/skills` 为空）合并结果与改造前完全等价，T07 第 ③ 条锁住 |
| 模型臆造 resource ref | 提示词注入候选清单 + 发布期 `E_BINDING_MISSING` 拦截并定位节点 |
| system 可编辑后注入风险 | `input_guard` 已清包根自动加载文件；system 走 `system_injected` 标记留痕（P1-T15 已有） |

## 6. 本线不做

- 节点级模型选择（另立线，涉及网关 key 与计费口径）。
- 节点级预算（预算仍在 run 级 + `paused_budget`）。
- 节点级 capability 细分（`NODE_CAPABILITIES` 白名单已够）。
- 节点级触发/重试策略定制（retry 字段已支持，不做 UI）。
- 工作流级"排除某资源"语法。

## 7. 实现记录（2026-11-19）

| 落点 | 文件 | 说明 |
|---|---|---|
| T01 | `src/ao/manifest.py` | `bindings_for_node()`；`NODE_BINDING_LIMIT = 8`；节点级 ref 复用工作流级的 `_REF_RE` 校验；重复 ref 在 `validate_graph` 里静默去重 |
| T02 | `src/ao/executor.py` | `_tools_for()` + `_tool_cache`（key 是节点 id）；`binding.resolved` / `binding.skipped` 均带 `node_id`；`node_started` 把 `ctx.mcp/ctx.skills` 校正成节点级结果 |
| T03 | `src/ao/executor.py`、`src/wf_sdk/__init__.py` | `_tools_for_turn()`：显式 `mcp/skills` 覆盖当前 turn 且不写缓存；`agent()` 不再回落到 `ctx.mcp`（否则每 turn 都会拿工作流级默认值压掉节点绑定） |
| T04 | `src/ao/resources.py` | `_check_one()` 抽出单绑定校验；工作流级 issue `node=null`，节点级带 `node` 且文案指向"该节点" |
| T05 | `src/ao/planner/draft.py`、`orchestrator.py` | `EDITABLE_FIELDS` 增加 `system/mcp/skills`；`SYSTEM_MAX_CHARS = 4000`；`_write_package` 只写用户改过的 system（清空回默认） |
| T06 | `src/ao/web/app.py` + `templates/planner.html` + `base.html` | 节点卡片三件套；`_edits` 收 `system_{id}` / `mcp_{id}` / `skills_{id}`；`update_draft` 用 `_check_draft_node_bindings()` 保存即校验 |
| T07 | `tests/e2e/test_node_bindings.py` | 三节点（无绑定 / hr-db / hr-db+pdf-render）：各 turn 的 `mcp_servers` 不同、`binding.resolved` 可审计、`graph.py` 仍是 `G = graph(...)`、老包合并结果不变 |

与计划的差异：

1. **"MCP 同名由节点级覆盖"在当前数据模型下等价于"去重 + 沿用工作流级 scope/owner"**：节点级只有裸 ref，没有 spec 与 scope；若按节点级新建 Binding，会把私有绑定降级成 global 并解析到另一个同名资源。实现取"去重且在位保留工作流级声明"，语义仍是"该节点要用它"。
2. **新增 `binding.resolved` 事件**（每节点一条，非每 turn）：`binding.skipped` 只能证明"缺了什么"，无法证明"实际注入了什么"，T07 的可审计性依赖它。
3. **上限 8 双处兜底**：`manifest.validate_graph`（结构，API/包直投也拦）与 `draft.apply_edits`（可读报错）。计划只写了"发布期拒绝"，实际提前到保存期，报错更早。
4. **`tests/e2e/test_generation.py` 删掉了一条从未生效过的节点级 ref**（`mcp__roles__get`）：改造前 `_build_manifest` 把它写成 `[]`，改造后它真的生效并因资源不存在被 `E_BINDING_MISSING` 拦下——属于计划的预期行为，不是回归。
