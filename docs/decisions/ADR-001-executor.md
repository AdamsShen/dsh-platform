# ADR-001 执行器：wf_sdk DSL + LangGraph（PG checkpoint）

- 状态：已实现
- 日期：2026-11-13（记录补写于 2026-11-19）
- 相关：P1-T03 / P1-T04 / P2-T04；`docs/plans/spec/2026-11-13-p1-execution-kernel.md` §3 D1

## 背景

MVP 的执行器是自写 DAG 解释器，直接读 `workflow_versions.manifest_json`。要支持"模型生成的工作流"就必须解决两个问题：生成物必须受控（不能是任意 Python），崩溃恢复必须精确到节点（不能整图重跑）。

## 决策

1. 生成物是**受控 DSL**：`wf_sdk` 只暴露 `graph` / `node` / `agent` / `emit` / `parse_contract`，包的 `graph.py` 只有 `G = graph(nodes=[...], edges=[...])`。
2. 平台把 DSL 编译成真正的 LangGraph `StateGraph`：`executor.build_state_graph()`（`src/ao/executor.py:549`）。
3. 用 `thread_id=run_id` + `AsyncPostgresSaver` 做 PG checkpoint（`src/ao/executor.py:677`），恢复粒度是 super-step。
4. 节点入口先查 `run_steps(run_id, node_id, attempt)`：有 `native_id` 且 `host` 为本机则 resume，否则新开。

## 理由

- 生成物是数据（节点/边声明），AST 白名单只需审查少量 import 与调用形态，比审查任意 Python 可行。
- LangGraph 自带 checkpoint、中断、重放语义，省掉自写状态机；`thread_id=run_id` 与平台的 run 一一对应。
- 节点内崩溃只能整节点重跑（super-step 粒度），这被接受，因为节点内外部副作用统一走 `run_effects` 幂等（`src/ao/effects.py`）。

## 替代方案

| 方案 | 否决原因 |
|---|---|
| 保留自写解释器 | 恢复点在节点边界要自己实现，等价于重写 LangGraph |
| 直接执行模型生成的 Python | 无法保证受控，安全边界崩溃 |
| Temporal / Celery | 引入新基础设施，与"单机 PG 起步"冲突 |

## 后果

- 正向：生成、发布、执行三段解耦；`E_GRAPH_MISMATCH`（`src/ao/static_check.py`）保证 manifest 与代码一致。
- 负向：`graph.py` 仍由 `importlib.exec_module` 在主进程加载（`src/ao/executor.py` 的 `_load_graph_module`），隔离问题见 ADR-005 与 P1-T13。
- 兼容：`AO_EXECUTOR=interpreter` 的对照路径在 P2 收口后已移除，只保留 LangGraph。

## 验证

- `tests/e2e/test_node_bindings.py`：断言 `graph.py` 仍是 `G = graph(...)` 且经 `build_state_graph` 编译。
- `docs/STATUS.md`「已确认（2026-11-19，第二批）」第 1 条。
