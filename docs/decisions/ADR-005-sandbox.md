# ADR-005 沙箱边界：不自研进程沙箱，执行隔离交给 harness

- 状态：已实现（2026-11-19 部分推翻，缺口见下）
- 日期：2026-11-13 决策 / 2026-11-14 反转 / 2026-11-19 复核
- 相关：P1-T13 / T14 / T17、P2-T13；`src/ao/input_guard.py`、`src/ao/harness/_dsh_runtime.py`

## 背景

规格原案要求平台自建 bwrap/nsjail 启动器（P1-T13），并做输入中和、system 序言、出网白名单、能力包四件事。实测阶段发现：容器内非特权环境挂 `/proc` 会失败，需要 seccomp 放宽；而 DSH SDK 自带沙箱（macOS seatbelt / Linux）已经能表达 `read-only` / `workspace-write` 三档。

## 决策

1. **不采用平台自研的 bwrap/nsjail 层**，与 sortie-deck 对齐：执行隔离统一交给 harness 原生 permission mode。
2. 平台只做三件配合：
   - `capability` → DSH mode 的映射（`_SANDBOX_TO_DSH`），默认 `workspace_write`，`full_access` 不开放；
   - 每次 turn 前清理包根会被 harness 自动加载的文件（`src/ao/input_guard.py`），失败按 `sandbox_denied` 处理、不重试；
   - 沙箱不可用时的探测与降级告警（`dsh_sandbox_auto_fallback`）。
3. system 序言注入并落 `node.meta.system_injected`。

## 2026-11-19 复核结论（重要）

上述决策**只覆盖 harness 会话内的执行**。`graph.py` 本身由 `importlib.exec_module` 在 ao 主进程加载，**不在 DSH 会话内**，DSH 的 permission mode 覆盖不到它。因此：

- P1-T13 的规格原案（子进程 + IPC）**仍然有效并已重新排期**（M5）；
- 影响面：生成代码可读全部环境变量（含上游 key）、可访问 PG 连接池、可读写整个文件系统；
- 当前缓解只有输入中和（防自我注入）与 AST 白名单（挡明显危险调用）。

## 替代方案

| 方案 | 结论 |
|---|---|
| 自研 bwrap/nsjail（规格原案） | 容器内不可用点太多（挂 `/proc` 失败、需 seccomp 放宽、`--unshare-all` 断网），维护成本高于收益 |
| 每任务一容器 | 冷启与资源成本不可接受，规格明确排除 |
| 子进程 + IPC（仅隔离 `graph.py`） | **采纳为后续方案**，见 M5/P1-T13 |

## 后果

- 正向：删掉 `src/ao/sandbox.py` 全文与 `AO_SANDBOX*` 配置，减少一套需持续跟进内核差异的代码。
- 负向：存在明确已知缺口（`graph.py` 未隔离），不得对外宣称"生成与执行都在沙箱"（需求 #7 保持 🟡）。
- 实测记录保留：`docs/reports/2026-11-14-sandbox-matrix.md`（容器 vs 裸机两套参数）。

## 验证

- `tests/e2e/test_turn_input_guard.py`：包根放入 `CLAUDE.md` 后 turn 前被删除；不可删时节点失败。
- `scripts/probe_dsh_sandbox.py`：workspace-write / read-only 四组行为探测。
