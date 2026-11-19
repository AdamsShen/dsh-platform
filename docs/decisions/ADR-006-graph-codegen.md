# ADR-006 graph.py 生成方式：固定骨架 + 声明式补丁

- 状态：已实现
- 日期：2026-11-13（记录补写于 2026-11-19）
- 相关：P2-T04 / P2-T05；`src/ao/codegen.py`、`src/ao/static_check.py`

## 背景

P2 要求"模型生成可运行的工作流"，但生成物必须可审查、可复现、不能每次都换一种写法。让模型直接写完整 Python 文件会出现：import 漂移、模块级副作用、节点函数体里内联提示词、同一 manifest 两次生成字节不一致。

## 决策

1. **骨架固定**：imports、`G = graph(nodes=[...], edges=[...])`、`if __name__ == "__main__"` 都由模板产出；模型只决定节点/边。
2. 每个节点函数体只有一行：`return await agent(ctx)`；提示词不进代码，由 SDK 按节点 id 读 `prompts/<node>.md` 与 `<node>.system.md`。
3. 生成后立刻**反向解析**：`ast.parse` 抽出节点 id 集合与边集合，与 manifest 对账，不一致拒绝（`E_GRAPH_MISMATCH`）。
4. 骨架带版本号 `SKELETON_VERSION = 1`（`src/ao/codegen.py:14`），写进生成文件头部；加载器校验，不匹配拒绝运行。
5. 节点 id 含 `-` 时由 `py_name()`（`codegen.py:28`）映射成合法标识符，不改变 manifest 里的 id。

## 理由

- "清单是真值、代码是产物"：改提示词/绑定不需要重新生成代码（P2-T09 的前提）。
- 骨架固定让生成的 `graph.py` 可 diff、可字节比对（幂等），也让 AST 白名单只需盯少数形态。
- 反向解析是最后一道自检：即使模型绕过模板，节点/边也会与 manifest 对账失败。

## 替代方案

| 方案 | 否决原因 |
|---|---|
| 模型整篇写 `graph.py` | import 与副作用无法约束；两次生成不一致，hash 每次都变 |
| 不生成代码，运行时按 manifest 解释 | 与 ADR-001 的 LangGraph 编译路径冲突，等于回到 MVP 解释器 |
| Jinja2 模板 + 自由表达式 | 表达式可逃逸，白名单收不住 |

## 后果

- 正向：`graph.py` 的 mtime/hash 在"只改提示词"时不变，可直接作为验收断言。
- 负向：不支持 `action` 等非 LLM 节点（2026-11-14 已彻底移除 action）；复杂控制流（循环/分支表达式）只能靠 `edges` 的 `when`，表达能力有限。
- 约束：修改骨架必须升 `SKELETON_VERSION`，否则旧包会被误判为可运行。

## 验证

- `tests/unit/test_codegen.py`：同 manifest 生成两次字节一致。
- `tests/unit/test_static_check.py`：`E_GRAPH_MISMATCH` 正反例各一。
