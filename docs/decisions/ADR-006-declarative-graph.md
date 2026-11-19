# ADR-006 声明式工作流：每阶段一个 harness 会话，跨阶段只靠文件

- 状态：已实现（2026-11-19）
- 取代：ADR-005 的"`graph.py` 在主进程执行"遗留缺口、M5 的 W50/W51 子进程方案
- 相关：`src/ao/executor.py`、`src/ao/memory.py`、`src/ao/static_check.py`、`src/ao/package.py`
- 上游决策来源：sortie-deck `docs/角色Agent设计问答与业界调研.md:240`

## 背景

两条既有事实互相矛盾：

1. `graph.py` 是**平台模板渲染**的产物（`codegen.generate_graph`），节点体恒为
   `await agent()` + `parse_contract()`，没有第二种形态；
2. 它却在运行期由 `importlib.exec_module` 在 worker 主进程加载，继承 PG 连接池、
   上游 key 与整个文件系统。DSH 的 permission mode 只在 harness 会话进程内生效，
   覆盖不到这一层。

M5 原计划用"子进程 + IPC"把这段代码关进无凭据进程。但既然节点语义只有
"一次 harness turn"，更彻底的解法是不让生成代码存在。

## 决策

1. **不再生成 `graph.py`**。manifest.json 是唯一真值，执行器直接从
   `manifest.nodes/edges` 构图（`build_state_graph`）。旧包即使带 `graph.py` 也不加载，
   `REQUIRED_FILES` 只保留 `manifest.json`。
2. **一个阶段 = 一个新 harness 会话**。`native_id` 只在同一 `run_id + node_id + attempt`
   内复用；节点切换必然为 `None`，跨阶段零会话继承。
3. **阶段内多轮**在同一会话里续跑：首轮不合格就用 `repair_prompt` 反馈，
   直到满足契约或达到 `AO_STAGE_MAX_TURNS`（默认 3，含首轮）。
4. **跨阶段延续靠文件**：`memory.md`（阶段收尾由 LLM 提炼 3-5 行，滚动读尾部）
   + `artifacts/` 索引 + 上游节点产物，统一由 `memory.bootstrap_block()` 拼进首轮 prompt。
5. **静态检查对象换成声明**：`static_check.check_manifest()` 查节点重复、边端点、
   自环、`when` 语法、prompt 模板语法；AST 白名单随可执行代码一起退场。

## 替代方案

| 方案 | 结论 |
|---|---|
| 保留可执行 `graph.py` + 子进程 IPC（M5 W50/W51） | 放弃。代码自由度为零，却要付出 5–7 人日与一套 RPC 协议 |
| 全任务一个会话，持续 resume | 放弃。上下文膨胀、跨阶段污染，且崩溃后无法按阶段重跑 |
| 每节点一容器 | 冷启成本不可接受（规格已排除） |

## 业界对照

- Tarvos relay：每段工作起新 agent，读磁盘 plan，交棒写 Baton note；
- Hermes / Kanban：一阶段一卡一 fresh agent，board 是持久状态；
- LangGraph checkpointer：状态按 `thread_id` 保存，换 thread 即空白——跨阶段记忆必须外置。

## 后果

- 正向：需求 #7 从 🟡 转 ✅（平台侧不再有可执行生成代码）；M5 取消；
  `graph.py` 的版本 diff 噪音消失；阶段失败可单独重跑。
- 负向：旧文档/脚本若引用 `graph.py` 需更新；阶段摘要多一次模型调用
  （mock 模式自动跳过，`AO_STAGE_SUMMARY=0` 可关）。
- 兼容：旧包不迁移，manifest 字段未变。

## 边界：并行节点不做硬目录隔离

计划里 G4（并行节点各写 `workspace/<node_id>/`、包根只读）**本轮不实施**。
DSH 只有三档权限（`read-only` / `workspace-write` / `danger-full-access`），可写范围由 `cwd`
决定，没有额外可写目录参数（`src/ao/harness/_dsh_runtime.py:93`、`:781`）。硬隔离要么把
`cwd` 下沉并改全部 `artifacts/` 相对路径，要么每节点复制一份只读包，两者都超过当前收益
（生成器只产串行链）。保留约定：并行节点产物写 `artifacts/<node_id>/...`。

## 验证

- `tests/unit/test_stage_memory.py` 7 条：摘要追加、滚动只切整段、上游/产物注入、截断。
- `tests/integration/test_execute.py::test_cross_stage_fresh_session_and_in_stage_resume`：
  跨阶段 `native_id=None`，阶段内 repair 带上一轮 `native_id`，下一阶段 prompt 含上游产物。
- `tests/integration/test_execute.py::test_contract_failure_repairs_once_then_fails`：
  多轮用尽（`repair_count=2`）才判失败。
- `tests/integration/test_execute.py::test_node_turns_override_platform_default`：
  `manifest.turns=2` 时该阶段只许首轮 + 1 次续轮（覆盖全局默认 3）。
- `tests/integration/test_execute.py::test_stale_native_id_from_other_host_is_not_resumed`：
  `run_steps` 里别的机器的 `native_id` 不会被本机续用。
- `tests/e2e/test_generation.py`：冻结包内不存在 `graph.py`，页面与七步产物齐全。
- 全量 240 passed。
