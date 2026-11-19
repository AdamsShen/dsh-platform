# AO · DSH 编排平台（执行内核）

按 `auto-orchestration-platform/docs/superpowers/specs/2026-11-13-harness-orchestration-platform-design.md`
与同目录 `docs/superpowers/plans/2026-11-13-*.md` 的分步方案实现。

当前阶段：**P1 执行内核 + P2 工作流包 + P4 页面生成器（M4a/M4b）已落地**。实现状态见
`docs/STATUS.md`。

页面生成器一句话概述：页面真值是一份受控 Page Schema（`ui/pages/*.json`），
编译成 `ui/compiled/<page>/{index.html,app.js,theme.css}` 静态产物并随包冻结，
按 `workflow@version` 托管在 `/p/{workflow}/{version}/{page}`；编辑器与分享链接都
只改/只读 Schema，产物不可手改。

## 快速开始

```bash
uv sync --all-extras
make db-up                 # 本地 Postgres（端口 55433）
cp .env.example .env
uv run python -m ao.migrate
uv run python scripts/run_example.py    # mock provider 跑通两节点工作流
uv run python scripts/drill.py          # P1 验收演练：正常/失败/停止/接管/预算
uv run pytest -q
make resources                          # 注册示例 MCP / Skill（页面绑定面板用）
make run                                # 创作面 http://127.0.0.1:8000
```

`run_example.py` 最后会打印示例看板地址
`http://127.0.0.1:8000/p/daily_schedule/1/main`（先 `make run` 起服务）：
那是 `examples/daily_schedule/ui/pages/main.json` 编译出的真页面，数据来自
`/api/page/...` 五个只读数据源。

真实模型（需要 `deepseek-harness-sdk` 与 `DEEPSEEK_API_KEY`）：

```bash
uv sync --all-extras          # 装 harness extra（含 DSH 运行时，约 95MB）
AO_MOCK=0 AO_ENV=dev uv run python scripts/run_example.py
uv run python scripts/probe_dsh.py        # 单轮探针
uv run python scripts/capacity_report.py  # 容量采样
uv run pytest -m real                     # 真实 provider 用例（默认跳过）
```

注意：本机没有 LiteLLM 网关时，真实 run 的 `spent_usd` 恒为 0——账单以网关
spend log 为准，不按 provider 返回值估算。

## 目录

```
src/wf_sdk/        生成代码唯一可用的接口（node/graph/agent/action/emit）
src/ao/harness/    provider：mock + 移植的 DSH 运行时
src/ao/executor.py LangGraph 执行 + 节点记账/事件/fencing/repair
src/ao/queue.py    租约队列（SKIP LOCKED + 心跳 + 回收）
src/ao/state.py    状态机与失败分类
src/ao/gateway.py  LiteLLM 虚拟 key 与 spend log
src/ao/sandbox.py  bwrap/nsjail 参数构造（fail closed）
migrations/        顺序 SQL 迁移
examples/          手写工作流包（生成器的黄金样例）
```

## 硬约束

1. **不静默降级**：`AO_MOCK=0` 时 SDK 缺失、网关未配置、沙箱不可用都直接失败。
2. **账单只信网关**：`llm_usage` 是网关流水的镜像，`runs.spent_usd` 由聚合刷新。
3. **写库带 epoch**：旧 worker 的写入会被 fencing 拒绝。
4. **停止只在节点边界**：DSH 单轮阻塞且无 cancel。
5. **生成代码只能用 `wf_sdk`**：平台在 AST 层拒绝其它 import（P2）。

## 已知简化

| 简化 | 影响 | 后续 |
|---|---|---|
| 包仓库只用本地目录 | 多机共享需对象存储 | P2-T03 |
| mock 模式按价目表估算 | 账单仅供演示 | P1-T12 已接网关适配器，待真 key 验证 |
| 沙箱参数已构造，未在容器内实跑 bwrap | macOS 无 bwrap | 部署到 Linux 后验证 |
| 冒烟只跑 mock 成功路径 | 失败分支待补 | P2-T06 |
| Web 控制面 A+B+C 期 | 对话 / 草稿编辑 / 发布 / 工作流级绑定 / cron 定时触发 / 事件入站 / 错过时刻回填（窗口+上限）/ 重叠策略（skip、buffer_one、cancel_other）/ 手动 backfill 可用；无 terminate_other 与 allow_all（与"节点边界停止"、无并发上限冲突） | 按需再评估 |
