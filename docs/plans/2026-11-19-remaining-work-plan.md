# 剩余工作总计划（2026-11-19）

> **本文件定位**：执行计划，不是新规格。规格以 `docs/plans/spec/`（P1~P4）与 `docs/plans/2026-11-19-multi-user-fanout.md`（P5）为准，
> 状态以 `docs/STATUS.md` 为准。本文件只回答三个问题：**还剩什么、先做哪个、做完怎么验**。

## 0. 口径

- **需求口径**：#n 指设计 §0 的 14 条需求（清单见 `docs/STATUS.md:110` 起的「需求覆盖总表」）。这是唯一验收目标。
- **任务编号**：沿用各线原编号（P1-T13、P5-T03…），不改语义。跨线的排期单元用 **W-xx** 表示，W 只是"这一周做这几件"的包装，不与规格编号冲突。
- **完成定义**（四件齐才算 ✅，缺一件记 🟡）：
  1. 代码合入 `src/`；
  2. 有自动化测试或脚本化验收（命令可复跑）；
  3. `docs/STATUS.md` 对应行回写「实现位置 + 验收命令 + 实测结论」；
  4. 已提交（不能长期挂在工作区）。
- **估时单位**：人日，按单人开发计；区间下限=顺利，上限=含返工与真机验证。

## 1. 基线（2026-11-19 实测）

| 项 | 事实 | 来源 |
|---|---|---|
| 测试 | `uv run pytest -q` → **136 passed, 1 deselected**（19.7s） | 本次实跑 |
| 路由 | 13 条，无 `/runs/{id}/cancel`、无 `/runs/{id}/retry`、无 `/audit` | `src/ao/web/app.py:166` ~ `:342` |
| 触发器 | `fire_due:169` → `_plan_slots:247` → `_spawn:328`；重叠判定 `_running_run:478` 只按 `workflow_id` | `src/ao/triggers.py` |
| 迁移 | 已到 `011_gateway_keys.sql`，**无 012** | `migrations/` |
| 资源层 | `resources.py` 只有可见性（`list_available:26` / `check_bindings:115`），**无 readonly 契约字段** | `src/ao/resources.py` |
| 已闭环链路 | 对话贴需求 → 七步生成 → 改提示词/系统词/节点级 MCP+Skill → 发布冻结包 → cron/event 触发（**单主体**）→ LangGraph 执行 → 租约+fencing 接管 → 网关虚拟 key 记账 | — |
| 版本沉淀 | HEAD `5eb7d64`（2026-11-14）；**P6 全部代码 + STATUS 仍在工作区**：11 文件改动 +571/−83、5 个未跟踪文件 | `git status` |

**一句话定位**：「一个」已经能跑完，「一批」还没开始；界面侧（P4）零代码；运维面（停止/重试/审计）有内核无入口。

## 2. 未完成清单（唯一真值）

| 编号 | 内容 | 现状 | 归属里程碑 |
|---|---|---|---|
| P1-T13 | `graph.py` 子进程 + IPC 隔离 | 🟡 决策已反转，规格原案有效 | M5 |
| P1-T16 | 出网白名单 + 凭据代理 | ❌ 零实现（`egress`/`allowlist` 全仓零命中） | M6 |
| P1-T17 | capability 档位 + MCP 只读契约 | 🟡 有档位，无注册期只读校验 | M6 |
| P2-T10 | 派生与克隆（clone API） | ❌ 未实现（`grep clone src` 零命中） | M6 |
| P2-T20 | 复用派生端到端验收（规格原 T16） | ⬜ 依赖 T10 | M6 |
| P3-T02 | 用户/角色/API Key/租户配额 | ⬜ 仅建表（`migrations/001_init.sql:24`） | M2（可选） |
| P3-T04/T05/T06/T07/T08/T09/T10 | 配额公平调度 / SSE / 观察者 / 成本报表 / 审计 / 分区 / 压测 | ⬜ 除 T08 的审计路由外**本期不做** | 后续（见 §7） |
| P4-T01~T11 | 前端生成器（Schema→编译器→静态代码→托管→编辑→分享） | ⬜ 全部 | M4a / M4b |
| P5-T01~T08 | 用户目录 / 扇出模型 / 展开 / 重叠 / 预算 / 用户费用 / 对象存储 / 端到端 | ⬜ 全部 | M1 / M3 |
| D1 | 需求 #5「harness 按变量查 MCP」口径差异 | 待定，当前为平台侧预取 | 待决策，不排期 |
| D2 | `docs/decisions/` 为空，规格要求的 ADR-001~017 未落盘 | 文档缺口 | M0 |
| D3 | 规格引用 `scripts/demo.py` 不存在（实际为 `run_example.py` + `Makefile:29 demo`） | 文档缺口 | M0 |

## 3. 里程碑与工作项

### M0 · 收口与地基化（1 人日）

| W | 内容 | 改动 | 验收 |
|---|---|---|---|
| W01 | 提交现有工作区，拆两个 commit：`feat(bindings)` 代码 + `docs(status)` 文档 | — | `git status` clean；`make test` 绿 |
| W02 | 补 ADR：只写**已决策**的（ADR-001 执行器、ADR-002 PG 访问、ADR-003 包存储、ADR-005 沙箱、ADR-006 代码生成、ADR-010 SSE、ADR-011 调度器、ADR-012 公平队列），未开工的随对应里程碑补 | `docs/decisions/*.md` | 目录非空，每条含"决策/理由/替代方案/后果" |
| W03 | 修正失效引用：`scripts/demo.py` → `make demo` / `scripts/run_example.py`；在 STATUS 顶部标注 | `docs/STATUS.md`、spec 文首注记 | 规格里的命令能直接跑 |

**门禁**：仓库干净 + ADR 有 8 份 + 全量测试绿。**这是后续所有里程碑的起点，不做完不进入 M1。**

---

### M1 · 扇出闭环（P5 A 期，10–11 人日）→ 需求 #9 #6

目标：一条 cron 计划 → N 个用户各一个独立 run，单用户故障隔离，费用可按用户拆。

| W | 任务 | 关键改动 | 验收（可命令） | 估时 |
|---|---|---|---|---|
| W10 | **P5-T01** 用户目录接线 | `migrations/012`（`users.external_ref`）、新增 `src/ao/users.py`（`list_active_users`）、`POST/GET /api/users`（仅运维）、seed 脚本 | 租户下 3 个 active 用户可列出，`rejected` 不出现 | 2d |
| W11 | **P5-T02** 扇出数据模型 | `012_fanout.sql`（`fanout_mode/spec/limit`、`budget_per_run`）、`manifest.Trigger.fanout` 发布期校验、`triggers.project:54` 落新字段 | `fanout_mode='bad'` / `limit=0` 在发布期被拒 | 1d |
| W12 | **P5-T03** 展开与建 run | `triggers.py` 新增 `_expand_subjects` + `_spawn_batch`；`window` 带主体后缀；单主体失败不回滚；一批只写一条汇总 `trigger_events` | 10 用户 × 1 时刻 → **10 个 run**，`subject->>'user_id'` 互不相同；重复触发不新增 | 3d |
| W13 | **P5-T04** 重叠按主体 | `_running_run:478` 加 `user_id` 参数；`buffer_one` 扇出下降级为 `skip` + warn + 审计 `detail` | 用户 A 在跑时 B 照常触发；A 自己按策略 skip | 1.5d |
| W14 | **P5-T05** 预算与限流 | `budget_per_run` → `runs.budget_usd`；`fanout_limit` 超限**整批拒绝**；`AO_FANOUT_BUDGET_CAP` 兜底 | 1200 用户 / limit=1000 → 0 个 run + `failed` 审计 + `detail="fanout_limit_exceeded: 1200>1000"` | 1.5d |
| W15 | **P5-T08** 端到端验收 | `scripts/drill.py` 增 5 个扇出场景（1000 扇出 / 1h 终态 / 单用户重投幂等 / 单用户卡住 / 费用可拆） | `make drill` 11/11 通过 | 2d |

**门禁**：
1. `uv run pytest -q` 全绿且新增扇出测试 ≥ 8 条；
2. `make drill` 覆盖 5 条扇出场景；
3. 实测 100 用户扇出：run 数 = 100，`queued` 1 小时内清空；
4. `docs/STATUS.md` 的 #9 由 ❌ 改 ✅，P5-T01~T05 标 ✅。

---

### M2 · 运维面（2–3 人日，可与 M3 并行）→ 需求 #12

| W | 任务 | 关键改动 | 验收 | 估时 |
|---|---|---|---|---|
| W20 | 停止入口 | `POST /runs/{id}/cancel` → 复用 `state.request_cancel`（只置标志，节点边界生效） | 点停止 ≤ 1 个节点耗时；不再出现 `node.started` | 0.5d |
| W21 | 重试入口 | `POST /runs/{id}/retry` → `state.resume` / 新建 attempt | 失败 run 重试后 `attempt` +1，事件可查 | 0.5d |
| W22 | 审计查询 | `migrations/013_operator_actions.sql` + `GET /audit?actor=&object=&from=&to=`；写操作统一落 `operator.action`（含 `result=denied`） | operator 停他人 run → 审计可查；越权尝试也落库 | 1–2d |
| W23 | **可选** P3-T02 用户/角色/API Key | `src/ao/auth.py`、`api_keys.py`（`sk_` 前缀、库存 sha256、明文只返回一次）、`pending/active/rejected`、最后 admin 保护 | pending 用户 token 立即失效；operator 改 global 返回 403；删最后 admin 返回 409 | 3–4d |

**门禁**：13 条路由 → 16 条；页面按钮可用；审计表有数据。W23 若跳过，#6 保持 🟡 并在 STATUS 注明"待多用户开放前补"。

---

### M3 · 费用与跨机（P5 B 期，5–6 人日）→ 需求 #11 #10 #14

| W | 任务 | 关键改动 | 验收 | 估时 |
|---|---|---|---|---|
| W30 | **P5-T06** 用户维度费用 | `llm_usage.user_id` + 回填 + `idx_llm_usage_user`；`usage.record(user_id=)`；`summary_by_user()`；费用页「按用户」 | 同批 A/B 费用可分开查，两者之和 = 批次总额 | 2d |
| W31 | **P5-T07** 对象存储 | `AO_PACKAGE_BACKEND=local|s3`；`package.resolve_ref` 支持 `s3://` 并落本地缓存；`freeze_package` 上传 | 两个独立进程从同一 `package_ref` 各跑通一次 | 3d |

**门禁**：`AO_PACKAGE_BACKEND=s3` 下 `make demo` 通；跨机接管演练（drill 的 takeover 场景换第二进程）通过；#10 #14 由 🟡 改 ✅。

---

### M4 · 前端生成器（18–24 人日，拆两段）→ 需求 #1

规格见 `docs/plans/spec/2026-11-13-p4-page-generator.md`，**产物是真代码**（`index.html` + `app.js` + `theme.css`），A 档无框架静态站点。

#### M4a · 渲染骨架（11–14 人日）——目标：一条 Schema 变成一个能打开的页面

| W | 任务 | 验收 | 估时 |
|---|---|---|---|
| W40 | P4-T01 调研（代码生成 vs 运行时渲染三档对比，含 amis 维护状态） | 结论附来源，确认 A 档 | 1d |
| W41 | P4-T02 Page Schema v1 契约（组件/数据源/动作白名单 + 6 个错误码） | `tests/unit/test_page_schema.py` 每码至少一例 | 3d |
| W42 | P4-T03 编译器（Schema → 静态代码，确定性构建） | 同一 Schema 编译两次**字节一致**；输入含 `onclick:"alert(1)"` 编译失败；产物无 `eval(` / 无 `on*=` | 5d |
| W43 | P4-T04 托管（`/p/{workflow}/{version}/{page}` + iframe `allow-scripts` 无 `allow-same-origin` + 5 分钟 token + CSP） | iframe 内 `document.cookie` 为空；旧版本路径仍可用 | 2–3d |
| W44 | P4-T05 数据源（`run.list/get/events`、`artifact.read`、`mcp.read`，强制 tenant + RBAC，单页 ≤200 行） | 跨租户 id 返回 404 | 2d |
| W45 | P4-T06 动作（`run.trigger/cancel/retry`、`node.invoke` + 幂等键） | 重复 `idempotency_key` 只执行一次；分享态 403 | 2d |

**M4a 门禁**：手写一份「日程看板」Schema → 打开可用页面 → 数据来自平台 API；`make demo` 增加"打开页面"一步。

#### M4b · 编辑与分享（7–10 人日）——目标：改列/换组件/分享链接

| W | 任务 | 验收 | 估时 |
|---|---|---|---|
| W46 | P4-T07 编辑器 v1（树 + 属性面板 + 上移/下移/删除/复制 + 即时预览） | 加列/换组件/加动作各一次；保存后 `manifest.json`/`graph.py` hash 不变 | 3d |
| W47 | P4-T09 页面生成接入七步（第 ④ 步产完整 Schema → 编译 → 冒烟 DOM 断言） | 对话产出带表格+详情的页面；冒烟失败给出缺失组件名 | 2–3d |
| W48 | P4-T10 免登录分享（`share_links` 表已存在，补 `src/ao/share.py` + `/s/{token}` + scope 编译成 SQL 谓词） | 未登录可读；动作 403；scope 外 404；撤销/过期 404；超 `max_uses` 429 | 2–3d |
| W49 | P4-T08 对话式改页面 + P4-T11 交付验收 | "表格加一列花费"只改 Schema；分享页无按钮无成本字段 | 2d |

**M4b 门禁**：`docs/STATUS.md` #1 由 ❌ 改 ✅；README「已知简化」移除"前端=固定模板页"。

---

### M5 · 执行隔离（P1-T13，5–7 人日）→ 需求 #7

| W | 任务 | 验收 | 估时 |
|---|---|---|---|
| W50 | `graph.py` 子进程化：`multiprocessing`/`subprocess` 加载冻结包，父进程 pipe 收事件；子进程死不影响 worker | 子进程内 `os.environ` 读不到上游 key；子进程被杀 run 仍可被接管 | 3–4d |
| W51 | 只读挂载 + 最小环境变量白名单 + 进程组 `killpg` | 写 `/etc`、写 `$HOME` 失败；`pgrep -f <run_id>` 为空 | 2–3d |

**门禁**：新增 `tests/e2e/test_graph_isolation.py`；`docs/reports/2026-11-14-sandbox-matrix.md` 结论更新（容器 vs 裸机两套启动参数）。

---

### M6 · 安全与复用（6–8 人日）→ 需求 #13 #8

| W | 任务 | 验收 | 估时 |
|---|---|---|---|
| W60 | P1-T16 出网白名单 + 凭据代理（新增 `src/ao/sandbox/egress.py`） | `curl https://example.com` 失败；`curl <gateway>/health` 成功；MCP patch 文件节点结束即删（0600） | 2–3d |
| W61 | P1-T17 MCP 只读契约（`resources` 表加 `readonly` 列 + 注册期契约测试；`danger-full-access` 出现即告警） | 声明 `workspace_write` 生效；试图 `full_access` 发布被拒 | 1–2d |
| W62 | P2-T10 克隆（`POST /workflows/{id}/clone`，`parent_id` 追溯，user 级资源复制、凭据需重新授权）+ P2-T20 端到端 | 克隆体改提示词/绑定不影响原件；缺凭据发布告警且阻断 | 3–4d |

**门禁**：需求 #13 #8 改 ✅；`docs/STATUS.md` 无 ❌ 行（除明确不做的 P3 项）。

## 4. 依赖与关键路径

```
M0 ──> M1（扇出） ──> M2（运维面）──┐
        └──> M3（费用/跨机）────────┤
                                   ├──> M4a（渲染骨架）──> M4b（编辑/分享）──> M6（安全/克隆）
                                   └──> M5（隔离，可与 M4 并行，但需 M1 之后腾出排期）
```

- **关键路径 = M1 → M4a → M4b**（前端体量最大，18–24 人日）。
- M5 与 M4 无代码耦合，可交错做（改 `executor.py` 时顺带做子进程化，冲突面小）。
- M6 的 W62 只动 planner/workflow 层，随时可插入。

## 5. 里程碑门禁命令（统一入口）

| 场景 | 命令 |
|---|---|
| 全量测试 | `uv run pytest -q` |
| 演练（正常/契约失败/取数失败/停止/接管/预算 + 扇出 5 条） | `make drill` |
| 单机全链路 | `make db-up && make migrate && make demo` |
| 扇出专项 | 新增 `uv run python scripts/drill_fanout.py`（W15 产出） |
| 隔离专项 | `uv run pytest tests/e2e/test_graph_isolation.py -q` |
| 前端专项 | `uv run pytest tests/unit/test_page_schema.py tests/e2e/test_hosting.py -q` |

## 6. 风险与对策

| 风险 | 对策 |
|---|---|
| 1000 用户 × 4 节点 ≈ 4000 turn 打满执行池，白天对话被挤 | 创作池/执行池已物理隔离；M1 门禁里实测吞吐后调 `AO_EXEC_SLOTS` 或把 `scheduled_for` 打散到分钟 |
| P4 体量最大且是全新子系统，容易"做到一半没有可用物" | M4a/M4b 硬切：M4a 只交付"一份 Schema → 一个能打开的页面"，编辑器与分享不进 M4a |
| 编号继续撞车（P2-T16 vs T20、P3-T04 与 P5 重叠） | 本计划统一用 W-xx 排期；改 STATUS 时同步「编号映射」表 |
| 进度再次只存在于工作区 | 完成定义第 4 条：每完成一个 W 必须提交一次，commit message 带 `W-xx` |
| 文档失真重演（"做了未登记""登记了没做"） | 每个里程碑收尾强制回写 STATUS 并附验收命令输出；不接受只有日志截图 |
| 规格里的验收命令跑不通（`scripts/demo.py`） | M0-W03 先修，避免后续每个门禁都要手工换算 |

## 7. 本计划明确不做（避免范围蔓延）

- **P3-T04 / T05 / T06 / T07 / T09 / T10**（配额公平调度、SSE、观察者问答、成本报表、事件分区、1000 任务压测）：无外部用户前收益低于成本，等 M4 交付后按需启动。仅做 T08 的审计路由（W22）。
- **需求 #5 口径调整**（harness 驱动取数 → 平台预取）：保持现状，标 🟡 说明差异，不排期。
- 多副本容灾编排、PG 自动 promote、每任务一容器、NFS 共享会话。
- 用户注册/登录/密码体系（`users` 表只当引擎侧主体目录，鉴权仍走签名 cookie，直到 W23 决定是否上）。
- 自由画布编辑器（P4 v2）、自定义主题编辑器、工作流市场。

## 8. 工作量汇总

| 里程碑 | 范围 | 估时 | 完成后需求变化 |
|---|---|---|---|
| M0 | 提交 + ADR + 文档修正 | 1 | — |
| M1 | P5 A 期（T01–T05 + T08） | 10–11 | #9 ❌→✅，#6 🟡→✅（目录层） |
| M2 | stop/retry/audit（+ 可选 P3-T02） | 2–3（+3–4） | #12 🟡→✅ |
| M3 | P5 B 期（T06–T07） | 5–6 | #11 #10 #14 🟡→✅ |
| M4a | P4-T01~T06 | 11–14 | #1 ❌→🟡 |
| M4b | P4-T07~T11 | 7–10 | #1 🟡→✅ |
| M5 | P1-T13 | 5–7 | #7 🟡→✅ |
| M6 | P1-T16/T17 + P2-T10/T20 | 6–8 | #8 #13 🟡→✅ |
| **合计** | | **47–60**（含可选 50–64） | **14 条中 11 条 ✅，剩余 #5 口径差异 + P3 延后项** |

## 9. 建议的第一周（如果只给 5 天）

1. **D1**：M0 全部（提交、ADR、文档修正）——半天即可完成，剩余时间开 M1-W10。
2. **D2**：M1-W10 + W11（用户目录 + 扇出数据模型，两个迁移一次写完）。
3. **D3–D4**：M1-W12（`_spawn_batch`，最重的一项）。
4. **D5**：M1-W13（重叠按主体）+ 开始 W14。

## 10. 状态跟踪

- 本文件的表格**不勾选**，避免出现第二份真值；状态只写 `docs/STATUS.md`。
- 每个 W 完成后：更新 STATUS 对应行 → 跑门禁命令 → 贴命令输出摘要 → 提交（`W-xx: 描述`）。
- 里程碑结束时在 STATUS 顶部「最后更新」写明：完成了哪些 W、哪些门禁通过、需求覆盖统计变化。
