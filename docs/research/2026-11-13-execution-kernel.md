# 调研：执行内核方案复核（P1-T01）

调研日期：2026-11-14（对应计划 `P1-T01 业内方案复核`，文件名沿用计划约定）

方法：`agent-reach` 的 Exa 检索 + 官方文档/issue 原文抓取，共 6 组关键词。
范围只覆盖计划点名的四件事：LangGraph checkpoint 恢复语义、`SKIP LOCKED` + 租约 + fencing、
LiteLLM 虚拟 key、bwrap/nsjail 在容器内可用性。
不评价模型质量，不覆盖 UI 与业务层。

结论分两部分：**业内事实**（附来源）与**对本项目的判定**（含差异与待办）。
差异项一律不标"已完成"，避免把"读到结论"当成"改完代码"。

---

## 0. 结论摘要

1. LangGraph 的 checkpoint 粒度是 **super-step（节点边界）**，不是节点内部。节点执行到一半崩溃，
   恢复时**该节点整体重跑**；只有同一 super-step 内已经成功的**其他**节点不会重跑。
   → 节点必须自幂等，不能假设"框架会从断点继续"。
2. `FOR UPDATE SKIP LOCKED` 只解决"两个 worker 同时抢同一行"，**不解决崩溃恢复**。可靠队列必须叠加：
   短事务领取、`lease_expires_at`、心跳续租、**fencing token**、外部副作用幂等键、重试上限。
3. LiteLLM 的 key 生命周期是 `/key/generate|info|update|delete|regenerate|list` 六个端点；
   `delete` 是**归档 + 缓存逐出**而非物理删除；spend 由网关自己累加到
   `LiteLLM_VerificationTokenTable`，明细在 `LiteLLM_SpendLogs`（批量写）；
   多副本部署必须共享 Redis，否则吊销与限流在不同副本间不一致。
4. `bwrap --unshare-all` 展开为 `--unshare-user-try --unshare-ipc --unshare-pid --unshare-net
   --unshare-uts --unshare-cgroup-try`，**user namespace 是 "try"**，不可用时跳过。
   容器内非特权场景的失败点通常在**随后的 mount**（"No permissions to create new namespace"
   或 mount 被拒），而 Docker 默认 seccomp 直接拦 `clone`/`unshare`。
5. 因此沙箱探测不能只看 `returncode`/`shutil.which`，必须做**隔离断言**（uid_map、能否看到宿主根）。
6. 本项目在 2、3 两项的主体实现已与主流一致（epoch fencing、重试上限、`/key/generate` +
   `/key/delete` + `max_budget` + `/spend/logs`），主要缺口是**外部副作用幂等键**、
   **沙箱能力探测**、**真实网关验证**，见 §5。

---

## 1. LangGraph：checkpoint 的粒度与"节点内崩溃"

来源：

- 官方文档《Checkpointers》 https://docs.langchain.com/oss/python/langgraph/checkpointers
- PR #8050（`durability=sync` 下 `put_writes` 未等待即落 checkpoint，崩溃后节点重跑）https://github.com/langchain-ai/langgraph/pull/8050
- PR #8055（pending writes 必须先于覆盖它的 checkpoint 持久化）https://github.com/langchain-ai/langgraph/pull/8055

官方原文要点：

- "A checkpointer saves a snapshot of graph state **at each super-step**"，super-step 是"图的一次 tick，
  该步调度的所有节点（可能并行）一起执行"。
- "**Fault-tolerance**: if one or more nodes fail at a given superstep, you can restart your graph from
  the last successful step."
- "**Pending writes**: When a graph node fails mid-execution at a given super-step, LangGraph stores
  pending checkpoint writes from any other nodes that completed successfully at that super-step.
  When you resume ... you don't re-run the successful nodes."
- interrupt 恢复同样是"**The interrupted node starts again from its beginning**"。
- 继续执行的调用方式是 `graph.invoke(None, config)`（加载最新 checkpoint，重试未完成的工作），
  而不是"用同一 thread_id 再 invoke 一次输入"。

两个 PR 说明了同一件事的两面：pending writes 与 checkpoint 的**写入顺序**如果不保证，
崩溃恢复时会出现"checkpoint 已落、writes 未落"，于是框架认为节点没跑完而重跑，
**外部副作用被重复执行**（PR 里用 email/order 举例，且明确是 host-dependent，取决于线程调度）。

对本项目的判定：

- 我们用 `AsyncPostgresSaver` + `thread_id = run_id`（`/Library/git-root/dsh-platform/src/ao/executor.py:619`、
  `/Library/git-root/dsh-platform/src/ao/executor.py:625`），接管后同 run 复用同一 thread，
  因此"从最近节点边界继续"这一层是成立的，和主流用法一致。
- 但**节点内部不续跑**是硬约束：`langgraph>=0.6` / `langgraph-checkpoint-postgres>=2.0`
  （`/Library/git-root/dsh-platform/pyproject.toml:12`）不提供节点内断点。
  我们现在的节点主要内容是 LLM 调用（重跑一次多花钱但语义可接受），一旦接入 action/工具类节点，
  必须自带幂等键。
- 框架侧不保证 exactly-once 副作用（PR #8050/#8055 就是反例），所以设计上不能把"不重复"寄托在
  checkpointer 上。
- 未验证项：我们没有显式设置 `durability`，用的是默认值；上述两个 PR 的竞态在 SQLite/内存 saver 上
  更容易复现，PG saver 的实际表现需要在 P1-T13 的 Linux 环境里跑一次崩溃注入才能下结论。

---

## 2. `SKIP LOCKED` + 租约 + fencing token

来源：

- Ghassan Aldarwish《PostgreSQL Job Queues Need Leases, Not Just SKIP LOCKED》 https://ghassan.de/en/articles/postgres-job-queue-leases
- Gabriel Anhaia《Your SKIP LOCKED Queue Can Run a Job Twice. Heartbeats and Fencing Tokens Make It Safe》 https://dev.to/gabrielanhaia/your-skip-locked-queue-can-run-a-job-twice-heartbeats-and-fencing-tokens-make-it-safe-34l4

业内共同结论：

1. `SKIP LOCKED` 的语义只是"跳过已被锁的行"，作用是避免两个 worker 抢同一行；它**不提供**崩溃恢复、
   不阻止旧 worker 晚提交、不决定重试时机。
2. **禁止在事务里干活**。反例是 `BEGIN; SELECT ... FOR UPDATE SKIP LOCKED; <调用远程 API>; DELETE; COMMIT;`，
   行锁寿命等于任务耗时，会占连接、阻塞清理、把恢复能力绑在数据库会话上。
   正确做法：领取与 `UPDATE ... SET status='running', lease_expires_at=now()+interval` 在**一条语句/一个短事务**
   里完成并立即提交，任务在事务外执行。
3. **fencing token（generation number）**：每次成功领取 `token = token + 1`，worker 持有自己那份；
   完成时 `UPDATE ... WHERE id = $1 AND token = $2`，旧 worker 匹配 0 行 → 抛 `LeaseLost` 并回滚。
4. **用 token 判断，不要用时钟判断**：如果租约过期但没人接手，原 worker 的结果仍然唯一且应被接受。
5. 心跳周期必须显著小于租约（文中示例 60s 租约 / 心跳续租）。
6. 外部副作用（发邮件、调第三方）**不能**指望数据库 fencing，必须让接收方按**从 job id 派生**的幂等键去重；
   不能用 attempt 派生，因为每次回收 attempt 都变。
7. 生产必备：领取查询里带上 `attempt < N` 的重试上限，以及对活跃行建部分索引
   （`CREATE INDEX ... WHERE status IN ('queued','running')`）。

对本项目的判定（对照 `/Library/git-root/dsh-platform/src/ao/queue.py`）：

| 要点 | 本项目现状 | 结论 |
|---|---|---|
| 短事务领取 | `claim_next` 单条 `SELECT ... FOR UPDATE SKIP LOCKED` + 紧随 `UPDATE`（`/Library/git-root/dsh-platform/src/ao/queue.py:18`） | 对齐 |
| 租约 + 心跳 | 30s 租约、10s 心跳（`/Library/git-root/dsh-platform/src/ao/queue.py:1`、`:59`） | 对齐（心跳 1/3 租约） |
| fencing | `epoch`，回收时 `epoch + 1`（`/Library/git-root/dsh-platform/src/ao/queue.py:74`）；所有写库带 `epoch` 条件（`/Library/git-root/dsh-platform/src/ao/executor.py:547`） | 对齐，且比文中示例更彻底（连事件写入都带） |
| 用 token 而非时钟 | `renew_lease` 比较 `worker_id + epoch`（`/Library/git-root/dsh-platform/src/ao/queue.py:48`） | 对齐 |
| 重试上限 | `retry_count >= 3` 进死信（`/Library/git-root/dsh-platform/src/ao/state.py:112`） | 对齐 |
| 外部副作用幂等键 | **无** | 缺口，见 §5 |
| 部分索引 | `idx_runs_status_retry` / `idx_runs_lease` 为普通索引（`/Library/git-root/dsh-platform/migrations/001_init.sql:110`） | 可接受（表量级小），量大后再换 |

---

## 3. LiteLLM Proxy：虚拟 key 与 spend

来源：

- 官方《Virtual Keys》 https://docs.litellm.ai/docs/proxy/virtual_keys
- 官方《Management API》 https://docs.litellm.ai/docs/proxy/configuration/api
- 官方《What is stored in the DB》 https://docs.litellm.ai/docs/proxy/db_info
- DeepWiki《API Key Management》 https://deepwiki.com/BerriAI/litellm/3.5.1-api-key-management

接口事实：

| 端点 | 用途 | 备注 |
|---|---|---|
| `POST /key/generate` | 签发 | 可带 `models`、`metadata`、`max_budget`、`budget_duration`、`tpm_limit`、`rpm_limit`、`max_parallel_requests` |
| `GET /key/info` | 查单个 key 的 spend 与配置 | key 级 spend 自动累加 |
| `POST /key/update` | 改限额/模型/元数据 | 可运行时降 `rpm_limit` |
| `POST /key/delete` | 吊销 | **归档**（`is_deleted=True`）+ 缓存逐出，不是物理删除，可审计 |
| `POST /key/regenerate` | 轮换密钥 | 保留元数据 |
| `GET /key/list` | 列举 | 按 user/team 过滤 |
| `GET /spend/logs` | 明细 | 底层 `LiteLLM_SpendLogs`，按批次写入 |

其他要点：

- spend 由网关按 `completion_cost()` 计算，累加到 `LiteLLM_VerificationTokenTable`（key）、`LiteLLM_UserTable`（user）、
  `LiteLLM_TeamTable`（team）；另有 `LiteLLM_Daily*Spend` 预聚合表供 UI 查询。
- **被网关拒绝的请求不写 SpendLogs**：无效/过期 key、超预算、模型无权限、未知模型、body 不可解析。
  带 provider 错误、guardrail 拦截、限流错误的请求仍会写。
- 多副本：必须共享 Redis，否则"限流计数"与"吊销"在不同 worker 上不一致。
- 可用 `litellm_settings.upperbound_key_generate_params` 给 `/key/generate` 的参数设上限，
  超限直接 400 而不是静默截断。

对本项目的判定（对照 `/Library/git-root/dsh-platform/src/ao/gateway.py`）：

- 已实现：`POST /key/generate`（`/Library/git-root/dsh-platform/src/ao/gateway.py:103`）、
  `GET /spend/logs` + `normalize_spend` + 按 `gateway_request_id` 去重
  （`/Library/git-root/dsh-platform/src/ao/gateway.py:118`、`:155`），并有 `mock` 实现
  （`/Library/git-root/dsh-platform/src/ao/gateway.py:55`）。
- 已实现（初稿曾误判为缺口，核对 `gateway.py` 后更正）：
  1. **吊销**：`revoke_key` 走 `POST /key/delete`，失败只告警不阻塞收尾
     （`/Library/git-root/dsh-platform/src/ao/gateway.py:112`）。
  2. **单 key 预算**：签发时传 `max_budget` 与 `duration`，402 映射为 `BudgetExceeded`
     （`/Library/git-root/dsh-platform/src/ao/gateway.py:99`、`:104`）。
- 真实缺口：
  1. **未对真实网关验证字段**：`/key/generate` 响应用 `key_name`/`token` 兜底取 `key_id`，
     `/spend/logs` 用 `spend`/`prompt_tokens`/`startTime` 取值（`:109`、`:137`），
     这些都是按文档推测的，接真实实例需要核对一次。
  2. **未要求多副本共享 Redis**，吊销与限流在副本间可能不一致。
  3. **被拒请求不计费的语义没落到对账逻辑**：我们按 `spend/logs` 求和，天然不含被拒请求，
     这在"平台侧限制 = 网关侧限制"的前提下才成立，需要文档层面写清楚。
- 这些是 P1-T12 从 🟡 转 ✅ 之前必须补的，不算 P1-T01 的代码改动。

---

## 4. bwrap / nsjail 在容器内是否可用

来源：

- bwrap 手册（Debian trixie / Ubuntu / Arch 同文） https://manpages.debian.org/trixie/bubblewrap/bwrap.1.en.html
- bubblewrap issue #505《bubblewrap inside unprivileged docker》 https://github.com/containers/bubblewrap/issues/505
- anthropics/sandbox-runtime issue #180 https://github.com/anthropics/sandbox-runtime/issues/180
- openai/codex issue #16211 https://github.com/openai/codex/issues/16211
- google/nsjail issue #78 https://github.com/google/nsjail/issues/78
- google/nsjail README（Troubleshooting） https://github.com/google/nsjail/

关键事实：

1. **`--unshare-all` 不是"强制全隔离"**。手册原文：
   "Currently equivalent with: `--unshare-user-try --unshare-ipc --unshare-pid --unshare-net --unshare-uts --unshare-cgroup-try`"。
   `--unshare-user-try` 的语义是"能建就建，不能就跳过"，`--unshare-cgroup-try` 同理。
   也就是说 **user namespace 失败不会立刻报 namespace 错误**，但后续 `--ro-bind` / `--bind` 需要
   CAP_SYS_ADMIN（在新建 user ns 内才获得），于是失败点变成 mount 报错。
2. 容器内无 userns 时的典型报错（bubblewrap #505 的实测）：
   `bwrap: No permissions to create new namespace, likely because the kernel does not allow
   non-privileged user namespaces.`（并提示 `kernel.unprivileged_userns_clone=1`）。
3. Docker 默认 **seccomp 会拦 `clone` / `unshare`**，AppArmor 也可能拦。issue 里给出的放行方式，
   按推荐度排序：
   - 精确放开 seccomp 中 clone 的 namespace 位（改掩码），而不是整体关闭；
   - `--security-opt seccomp=unconfined` + `--security-opt apparmor=unconfined`（最后的兜底）；
   - `--cap-add SYS_ADMIN`（范围过大，允许所有 namespace）；
   - `--privileged`（不推荐，等于没有隔离）。
4. `--privileged` 下 bwrap 确实能跑（#505 的第一段），但这正说明"能跑"与"隔离有效"是两件事。
5. nsjail 同类问题：`clone(flags=CLONE_NEWNS|...|CLONE_NEWUSER|...) failed ... Operation not permitted`；
   `--disable_clone_newuser` 需要 **以 root 运行**；cgroup 相关报错可用 `--disable_clone_newcgroup`
   或关掉 cgroup pids 限制（nsjail #78、README Troubleshooting）。

对本项目的判定（对照 `/Library/git-root/dsh-platform/src/ao/sandbox.py`）：

- `build_bwrap` 使用 `--unshare-all`（`/Library/git-root/dsh-platform/src/ao/sandbox.py:70`），
  其注释"一并隔离 net/pid/user/ipc/uts"（`/Library/git-root/dsh-platform/src/ao/sandbox.py:65`）
  **表述过强**：user 与 cgroup 是 "try"，可能被跳过。这条注释需要按手册改，否则后人会以为 uid 隔离一定生效。
- `build_nsjail` 用了 `--disable_clone_newcgroup`（`/Library/git-root/dsh-platform/src/ao/sandbox.py:91`），
  与 README 给出的兼容建议一致；但没有 `--disable_clone_newuser`，在非 root 容器里会直接失败。
- `ensure_available` 只做 `shutil.which`（`/Library/git-root/dsh-platform/src/ao/sandbox.py:51`），
  **不检查 namespace 能力**。这解释了为什么现在<b>看起来</b>可用（macOS 上 `AO_SANDBOX=none`，Linux 上没实跑），
  一旦上 Linux 容器，很可能在第一次 mount 时才炸。
- fail closed 的策略是对的（`AO_SANDBOX=none` 仅 `AO_ENV=dev`），与业内"宁可拒绝不可降级"一致。

### 容器内实测（Docker 28.5.2 / OrbStack，2026-11-14）

镜像 `docker.m.daocloud.io/library/python:3.12-slim` + apt 安装 `bubblewrap 0.12.0`，
命令统一为 `bwrap --unshare-all --ro-bind / / --tmpfs /tmp [--proc /proc --dev /dev]
/bin/sh -c 'id -u; cat /proc/self/uid_map'`：

| 容器配置 | 结果 | 输出 |
|---|---|---|
| 默认（builtin seccomp、无额外 cap） | 失败 | `bwrap: No permissions to create a new namespace, ...`（rc=1） |
| `--security-opt seccomp=unconfined`，保留 `--proc` | 失败 | `bwrap: Can't mount proc on /proc: Operation not permitted`（rc=1） |
| `--cap-add SYS_ADMIN`，保留 `--proc` | 失败 | `bwrap: pivot_root: Operation not permitted`（rc=1） |
| `--security-opt seccomp=unconfined`，去掉 `--proc`/`--dev` | 成功 | uid=0，uid_map=`0 0 1`（rc=0） |
| `--privileged`，保留 `--proc`/`--dev` | 成功 | uid=0，uid_map=`0 0 1`（rc=0） |

三点结论：

1. §4 事实 2 的推断被证实：**失败点在 mount**。默认 seccomp 下先死在 namespace 创建，
   放开 seccomp 后前移到 `mount proc`，再加 `CAP_SYS_ADMIN` 又前移到 `pivot_root`。
   同一份 argv 在不同容器配置下报三种错，说明**stderr 首行就是最有效的归因依据**。
2. 只加 `--cap-add SYS_ADMIN` **不够**（`pivot_root` 仍被拒），"加 SYS_ADMIN 就能跑"的说法在这套
   运行时不成立；实测能跑通的非特权配置是"放开 seccomp + 不挂 `--proc`/`--dev`"。
3. 成功时 uid_map 是 `0 0 1`（非 identity），`probe()` 的判定与真实形态一致；而"没建 userns
   却能跑"的 identity 场景在本机构造不出来，该分支目前只有单测覆盖。

对 `build_bwrap` 的直接提示：它固定挂载 `--proc /proc` 与 `--dev /dev`，在非特权云容器里
很可能正好死在这两个 flag 上，需要评估 harness 是否真的依赖 `/proc`。

---

## 5. 差异与待办（按优先级）

| # | 缺口 | 影响 | 落点 |
|---|---|---|---|
| 1 | 沙箱只查二进制存在，不探测 namespace/mount 能力 | P1-T13 在 Linux 容器里可能首次 mount 才失败，且失败点难以归因 | `sandbox.py` 增加 `probe()`：在 `--unshare-all` 下跑 `id -u` 与 `cat /proc/self/uid_map`，断言映射为 0；再断言读不到宿主 `/` 的非挂载目录 |
| 2 | `build_bwrap` 注释把 `--unshare-all` 描述为强制隔离 | 误导后续实现（尤其 authoring profile 的"不得跨 session"） | 改注释，并按需显式写 `--unshare-user`（要强隔离就别用 `-try`） |
| 3 | 外部副作用没有幂等键 | 节点重跑（LangGraph 语义 + 接管重跑）会重复发消息/写外部系统 | action 类节点的 key 由 `run_id + node_id` 派生，不用 attempt |
| 4 | 网关未对真实实例验证；未要求多副本共享 Redis | 字段推测错误会导致记账静默为零；副本间吊销不一致 | 起一个 LiteLLM Proxy 核对 `/key/generate` 与 `/spend/logs` 响应；部署文档写明 Redis |
| 5 | `durability` 未显式配置，崩溃注入未做 | 无法断言"节点边界不重复"在 PG saver 上成立 | P1-T13 的 Linux 环境里做一次 kill -9 注入 |
| 6 | 无部分索引 | 当前量级无影响 | 表增长后再评估，不提前优化 |
| 7 | `build_bwrap` 固定挂 `--proc`/`--dev` | 实测中非特权容器正是死在这两个 flag 上 | 评估 harness 是否依赖 `/proc`；不需要就按 profile 省略 |

以上 1、2、5、7 属于 P1-T13 的前置；3、4 属于 P1-T12 转 ✅ 的前置；6 不做。

已完成（2026-11-14）：

- 第 1 项：`sandbox.probe()` 实现，起一次最小 bwrap 读 `/proc/self/uid_map`，
  identity 映射（`0 0 4294967295`）判为"user namespace 未生效"，结果缓存 60s；
  `executor` 真跑路径改调 `require_available()`（二进制 + 隔离双检），失败即 fail closed。
- 第 2 项：`build_bwrap` 注释已按 bwrap(1) 手册修正，明确 `--unshare-all` 的 user/cgroup 是 `-try`。
- 第 5 项：仍需 Linux 环境做 kill -9 崩溃注入，本机无法完成。
- 第 7 项：`SandboxSpec` 新增 `mount_proc` / `mount_dev`（`AO_SANDBOX_MOUNT_PROC`、`AO_SANDBOX_MOUNT_DEV`，
  默认 1 = 保持原行为），`build_bwrap` 按开关决定是否挂载；`probe()` 固定挂 `/proc` 以便读 `uid_map`，
  与运行时开关无关。是否该长期关闭仍需在目标容器里实测 harness 对 `/proc` 的依赖后才能定。

---

## 6. 局限

- bwrap 已在本地 OrbStack 容器实测（见 §4 表），但**目标云容器仍未验证**；nsjail 完全未实测，
  只依据 README 与 issue 描述。
- 未覆盖 LangGraph `durability` 三档（`exit` / `async` / `sync`）的性能与语义差异，只确认了恢复粒度。
- LiteLLM 的 `/spend/logs` 返回结构随版本变动，`normalize_spend` 目前按 `spend`/`model`/`request_id` 取值，
  接真实网关时需要按实际响应核对一次。
