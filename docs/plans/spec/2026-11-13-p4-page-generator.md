# P4 · 前端生成器（M7 / M8 / M9）

> **迁移说明**（2026-11-19 自 `auto-orchestration-platform/docs/superpowers/plans/` 迁入）
> 正文保留原文未改。文中 `docs/superpowers/{research,specs,experiments,decisions}/...`
> 形式的路径指向**原仓库**，未随迁；已落地文档在 `dsh-platform/docs/{research,reports}/`。
> 本文件是**事前规格**（要做什么、如何验收），实际执行结果与偏离记录见 `docs/STATUS.md`。

日期：2026-11-13
上游：`2026-11-13-p2-workflow-generation.md`（manifest v2 与包的 `ui/` 目录）
覆盖：M7 渲染内核、M8 页面生成、M9 页面编辑与分享
前置：P2-T02（manifest v2 冻结）即可开工，不依赖执行内核细节；与 P2 后半程并行
估时：16–20 人日 → **修订后 18–24 人日**（见下节）

## 0. 修订（2026-11-19：D15 / D17 变更）

**触发**：需求方明确「通过自然语言生成的工作流需要在前端显示，**这个前端代码是自动生成的**」。
原 D15「Page Schema → amis JSON，运行期由 amis 渲染」产出的是**配置**，不是**代码**，不满足该要求。

**新决策**：

- 页面真值仍是受控 Page Schema（T02 契约不变）。Schema 只是 **IR**，不是给人改的代码。
- 编译器（T03）产出物从 `ui/compiled/<page>.json`（amis 配置）改为**可直接部署的静态前端代码**：
  `ui/compiled/<page>/index.html` + `app.js` + `theme.css` + `assets/`，随包冻结，按 `workflow@version` 托管。
- 生成物是**编译产物**，不参与编辑；改页面仍改 Schema（T07 / T08 完全不变）。
- 反目标边界收紧为：**禁止「LLM 直写前端源码、并以源码作为可编辑真值」**；
  「由编译器从 IR 生成真代码」不再属于反目标。设计文档 §4 反目标第 2 条的措辞需同步改为该表述
  （该文档在 `auto-orchestration-platform` 仓库，待下次修订一并更新）。

**受影响任务**：

| 任务 | 原方案 | 修订后 |
|---|---|---|
| T01 调研 | amis 维护状态与替代 | 增加「代码生成 vs 运行时渲染」选型：无框架静态站点 / Preact / React+Vite 三档的产物体积、构建时延、托管复杂度、可 diff 性 |
| T03 编译器 | 输出 amis JSON | 输出静态代码；组件白名单映射为生成的 DOM 构建函数；要求**确定性构建**（同一 Schema 两次构建字节一致） |
| T04 托管 | 页面壳 + amis 运行时 | 直接托管静态目录，iframe 加载 `index.html`，不再引入 amis 运行时；CSP / 短期 token / 版本不可变三项不变 |
| T09 页面生成 | Schema → 编译 → 冒烟 | 增加「构建真实代码」一步；冒烟改为加载生成的 `index.html` 做 DOM 断言 |
| T11 交付 | demo 打开页面 | 增加「产物可下载、可 diff」一步 |

**未受影响**：T02 Schema 契约、T05 数据源、T06 动作、T07 编辑器、T08 对话式改页面、T10 分享。

**估时变化**：16–20 → 18–24 人日（T03 +2，构建与产物托管 +2）。

**生成物形态选型（2026-11-19 已确认走 A）**：

| 档 | 产物 | 优点 | 代价 |
|---|---|---|---|
| **A（推荐）无框架静态站点** | 手写 DOM 构建函数 + 原生 JS + CSS 变量主题 | 零 Node 依赖（Python 直出）、产物小（< 100KB）、构建毫秒级、可读可 diff | 复杂交互（虚拟滚动、复杂图表）要自己写 |
| B Preact + htm | 生成的组件树 + 预编译 JSX | 体积 ~15KB，组件模型接近 React | 需要 esbuild/preact 构建链 |
| C React + Vite | 完整前端工程 | 生态最全 | 每页构建秒级、产物 MB 级、需 Node 工具链进镜像 |

选 A 的理由：本项目页面类型固定（表格 / 详情 / 表单 / 时间线 / 日历 / 卡片），
无框架足够；且 A 的产物能直接进 `ui/compiled/` 由 FastAPI 静态托管，不新增运行时依赖。
若后续出现复杂图表需求，可单独把「图表组件」降级为 B 档预编译资源，不影响主链路。

## 1. 目标与退出条件

**目标**：工作流自带可用界面，界面由受控 Page Schema 描述、可继续编辑（改列/换组件/加动作），并能生成只读分享链接。

退出条件：

1. 手写一份 Page Schema → 渲染出可用页面（表格 + 筛选 + 详情 + 时间线 + 表单），数据来自平台 API，不直连业务库。
2. 在编辑面板加一列"花费"、把表格换成卡片、给行加"重跑"按钮 → 不写代码、不改 Schema 原文，即时预览生效。
3. Schema 里出现白名单外组件、任意 URL、JS 表达式 → 发布被拒，错误码定位到节点。
4. 对话式改页面："表格加一列花费" → agent 只改 `ui/pages/*.json`，`manifest.json` 与 `graph.py` 不变。
5. 分享链接：未登录可只读打开；页面上没有触发/停止/重试/提交按钮；把 scope 外的 run id 拼进 URL → 404。
6. iframe 带 `sandbox="allow-scripts"` 且**不带** `allow-same-origin`；页面拿不到宿主 cookie。

## 2. 起点与终点

| 维度 | MVP 现状 | P4 终点 |
|---|---|---|
| 页面 | Jinja2 固定 8 个模板 | Page Schema 真值 + 编译到**静态前端代码**（2026-11-19 修订，原为 amis） |
| 数据 | 服务端模板直查 | 受控数据源（`run.list` / `run.get` / `run.events` / `artifact.read` / `mcp.read`） |
| 动作 | 表单 POST | 四种动作，写回只走 `node.invoke` |
| 编辑 | 无 | 树 + 属性面板 + 上下移动 + 即时预览 |
| 分享 | 整 run 只读 | 页面 + 显式 scope，只读无动作，可撤销，越权告警 |
| 托管 | 同源渲染 | 按 `workflow@version` 静态托管 + iframe 沙箱 + 短期只读 token |

## 3. 前提决策

| # | 决策 | 倾向 | ADR |
|---|---|---|---|
| D15 | 渲染器 | amis v6（设计已定），但**编译器与渲染器解耦**：`PageSchema → amis schema` 单独一层，可替换 | `ADR-014-renderer.md` |
| D16 | 表达式 | JSONata 子集 + `$` 上下文，编译期求值或转成 amis 的**受控**表达式；禁止透传任意 JS | `ADR-015-expression.md` |
| D17 | 托管形态 | 独立路径 `/p/<workflow>/<version>/<page>` + iframe；页面壳由平台渲染，内嵌数据来自 `/api/page/...` | `ADR-016-hosting.md` |
| D18 | Schema 版本 | `page_schema_version: 1`；升级时写 migration，不静默改旧页面 | `ADR-017-page-schema.md` |

## 4. 任务清单

### P4-T01 业内方案复核

产出 `docs/superpowers/research/2026-11-13-page-generator.md`：

- amis 当前维护状态与 v6 可用性；同类替代（LowCodeEngine、Formily、react-jsonschema-form）的取舍。
- LLM 生成"受控页面 Schema"的行业做法（组件白名单、数据源白名单、动作白名单的校验层设计）。
- iframe 沙箱托管多租户页面的安全实践（`allow-scripts` 无 `allow-same-origin`、postMessage 鉴权、CSP）。
- 低代码编辑器的树形编辑 vs 画布编辑的迁移成本。

工具：`agent-reach`、`github-search`、`Deep Research`；结论附来源。若检索不到 amis 的近期活跃证据，要明确写出并给出替代预案。

### P4-T02 Page Schema v1 契约

新增 `src/ao/pages/schema.py`（pydantic 模型 + 校验器）、`docs/superpowers/specs/page-schema-v1.md`。

- 组件白名单：`page / tabs / filter / table / list / card / detail / form / calendar / timeline / chart / text / image / modal / button`。
- 数据源白名单：`run.list` / `run.get` / `run.events` / `artifact.read` / `mcp.read`（只读工具）。
- 动作白名单：`run.trigger` / `run.cancel` / `run.retry` / `node.invoke`。
- 表达式：JSONata 子集，`$workflow` / `$selection` / `$filters` / `$user`；禁止任意 JS。
- 校验错误码：`E_COMPONENT` / `E_DATASOURCE` / `E_ACTION` / `E_EXPR` / `E_VERSION` / `E_REF`。
- 引用的 `source` 必须在该页 `data` 里声明；`onRowClick` 只能选已声明数据源。

验收：`tests/unit/test_page_schema.py` 每码至少一例；合法示例（日程看板）通过。

### P4-T03 编译器（Page Schema → 静态前端代码）

> ⚠️ 2026-11-19 修订：产物不再是 amis JSON，改为可直接部署的静态代码（`index.html` + `app.js` + `theme.css`），
> 见文首「0. 修订」。以下正文中「amis」相关映射按新决策理解为「生成的 DOM 构建函数」。

新增 `src/ao/pages/compiler.py`。

- `page` → amis `page`；`layout: sidebar+main` → `aside` + `body`。
- `table` → `crud`（只读模式）；`filter` → `form` + `submit` 绑定 `$filters`；`timeline` → `timeline`；`form` → `form` + `api` 指向 `node.invoke`。
- 表达式转换：`$filters.status` → amis 的 `${filters.status}`；**只允许**转换后的受控形态，遇到无法转换的表达式编译期报错。
- 编译产物为静态代码，落 `ui/compiled/<page>/`（`index.html` / `app.js` / `theme.css` / `assets/`），随包冻结；运行期不再编译。
- 生成的 `app.js` 只做三件事：按编译期写死的树挂载 DOM、按数据源白名单发 `GET /api/page/...`、按动作白名单发 `POST /api/page/.../action`。
  不生成 `eval`、不生成内联事件属性、不拼接未转义的插值——所有文本节点走 `textContent`。

验收：一份 Schema 编译两次字节一致；含 `onclick: "alert(1)"` 的输入编译失败；产物里不出现 `eval(` 与 `on*=` 内联属性。

### P4-T04 托管与沙箱

> ⚠️ 2026-11-19 修订：不再引入 amis 运行时，`/p/...` 直接返回生成的 `index.html`（静态目录挂载），其余（CSP / token / 版本不可变）不变。

新增路由 `/p/{workflow}/{version}/{page}`、`/api/page/{workflow}/{version}/{page}/data`。

- 页面壳由平台渲染：`<iframe sandbox="allow-scripts" src=...>`，**不给 `allow-same-origin`**。
- 注入短期只读 token（5 分钟，绑定 page + user + scope），数据请求带 token。
- CSP：`default-src 'self'; frame-ancestors 'self'`；amis 静态资源走同域。
- 按 `workflow@version` 静态托管，版本不可变；新版本新路径。

验收：`tests/e2e/test_hosting.py`：iframe 内 `document.cookie` 为空；跨版本访问旧路径仍可用；token 过期 401。

### P4-T05 数据源实现

新增 `src/ao/pages/datasources.py`。

- 每个数据源一个函数，入参经 Schema 声明 + scope 注入，不接受任意 SQL/URL。
- `run.list` / `run.get` 强制注入 `tenant_id` 与 RBAC 过滤；`run.events` 只返回允许的字段；`artifact.read` 只读对象存储、签名 URL 5 分钟；`mcp.read` 只调注册表里标 `readonly` 的工具。
- 响应裁剪：不含 `spent_usd`？——**看页面类型**：登录态页面可含成本，分享态不含（见 P4-T10）。
- 统一分页（`cursor`）与上限（单页 ≤ 200 行）。

验收：`tests/integration/test_datasources.py`：跨租户 id 返回 404；`mcp.read` 调非只读工具被拒。

### P4-T06 动作实现

新增 `src/ao/pages/actions.py`。

- `run.trigger` / `run.cancel` / `run.retry` 复用 P3 的接口，二次确认由 Schema `confirm: true` 控制。
- `node.invoke`：只能调 manifest 里已声明的 `action` 节点，入参过该节点 `contracts/<node>.json` 校验；平台保证幂等（`idempotency_key`）。
- 所有动作走 RBAC + `assert_owner`，落 `operator.action`。
- 分享态一律拒绝（见 P4-T10）。

验收：`tests/integration/test_page_actions.py`：`node.invoke` 调不存在的节点 400；重复提交同一 `idempotency_key` 只执行一次；分享态 403。

### P4-T07 编辑器 v1（树 + 属性面板）

新增 `src/ao/pages/editor.py` + 页面 `/workflows/{id}/pages/{page}/edit`。

- 左树：`page → blocks → columns/fields`，可选中；右属性面板按组件类型给字段。
- 操作：上移 / 下移 / 删除 / 复制；不支持自由拖拽（v1 明确不做画布）。
- 编辑结果写 `ui/pages/*.json`（新版本），即时预览（iframe 刷新 + 草稿编译）。
- "加一列"这类高频操作给快捷表单，不用手改 JSON。

验收：手工加列/换组件/加动作各一次；保存后 `manifest.json` 与 `graph.py` 的 hash 不变。

### P4-T08 对话式改页面

改动：`src/ao/planner/orchestrator.py`（新增"改页面"意图分支）。

- agent 只允许输出 Page Schema 的 patch（`blocks` 数组的增删改），不允许改 `data` 的 action 白名单。
- patch 过 P4-T02 校验 + P4-T03 编译 + 冒烟渲染，失败回滚并提示原因。
- 每次改动落 `workflow.published`（页面版本），可回滚到上一版。

验收：说"表格加一列花费" → 只改 Schema；说"加一个任意 URL 的表格" → 拒绝并解释原因。

### P4-T09 页面生成（M8）

> ⚠️ 2026-11-19 修订：冒烟从「编译成 amis JSON 后渲染」改为「编译并落盘静态代码 → 加载生成的 `index.html` 做 DOM 断言」。

- P2 的生成流程第 ④ 步改为产出完整 Page Schema（此前是最小占位）。
- 生成后：Schema 校验 → 编译 → 冒烟渲染（DOM 断言：关键组件存在、数据源可调）。
- 冒烟失败按 P2-T14 回第 ③ 步，最多 3 轮。
- 生成物随包冻结，`ui/compiled/` 一起进包。

验收：`tests/e2e/test_page_generation.py`：对话产出带表格 + 详情的页面；冒烟渲染失败时给出缺失组件名。

### P4-T10 免登录分享（M9）

新增 `src/ao/share.py` + 路由 `/s/{token}`、`/api/shared/{token}/data`。

建表（设计 §7.8）：

```sql
share_links(id, tenant_id, workflow_id, version, page_id, scope JSONB, token_hash UNIQUE,
            expires_at, revoked_at, max_uses, used_count, created_by, created_at)
share_access(id, share_id, page_id, ip, ua, status, in_scope, created_at)
```

实现要求：

- token 32B 随机 base64url，库存 sha256；放 path 不放 query；响应 `Referrer-Policy: no-referrer`。
- `scope` 必须显式：`{type: run|subject|list, ...}`；不接受"全部"，无 scope 不允许创建。
- 服务端把 scope 编译成 SQL 谓词，前端传来的 id 只作为 scope 内寻址参数。
- 只放行该 page 声明的数据源；响应不含 `tenant` / `spent_usd` / `native_id` / `lease_owner` / 错误堆栈 / 其他 run id。
- 动作全禁，无开关（表里没有 `allow_invoke`）。
- 有效期默认 7 天、上限 90 天；撤销立即生效（不缓存校验结果）。
- 校验失败统一 404；按 IP + token 前缀限速；`max_uses` 可设。
- 每次访问落 `share_access`；scope 外请求命中即告警。
- 页脚水印"由 <分享人> 分享 · 有效期至 <日期>"。
- PII 默认：只允许分享"结果视图"，不允许分享多主体列表页（服务端兜住）。

验收：`tests/e2e/test_share.py` 覆盖：未登录可读、动作 403、scope 外 404、撤销后 404、过期后 404、超 `max_uses` 429、越权命中产生告警事件。

### P4-T11 交付验收

- `scripts/demo.py` 增加页面与分享步骤：生成 → 发布 → 打开页面 → 编辑加列 → 生成分享 → 退出登录访问。
- 更新 `README.md` 的"已知简化"表（移除「前端 = 固定模板页」）。

验收：新机器一条命令走完；分享页无按钮、无成本字段。

## 5. 与相邻线的接口

| 依赖方 | 内容 |
|---|---|
| P2 | manifest v2 的 `ui.pages` / `ui.theme` 路径；包内 `ui/compiled/` 由本线写入，P2 只负责打包 |
| P3 | 数据源复用 `run.list/get/events` 查询层；动作复用触发/停止/重试接口；分享访问告警进 P3 告警通道 |
| P1 | `node.invoke` 执行 `action` 节点走 P1 的 action 载体与幂等保证 |

## 6. 风险与对策

| 风险 | 对策 |
|---|---|
| amis 停更导致无法修 bug | 编译器独立、产物是纯 JSON；必要时换渲染器只重写 `compiler.py`，页面与生成物不动 |
| 表达式转换漏口子导致 XSS | 只允许白名单转换 + 编译期拒绝；iframe 无 `allow-same-origin` 作为第二道防线 |
| 编辑后 Schema 与编译产物不一致 | 编译在发布时做，产物随版本冻结；运行期不编译 |
| 分享链接被转发到群导致数据外泄 | 显式 scope + 只读 + 水印 + 访问审计 + scope 外告警；默认不允许分享列表页 |
| 表单写回重复提交 | `node.invoke` 幂等键 + action 节点自身幂等（临时文件 + rename / upsert） |

## 7. 本线不做

- 自由画布编辑器（v2，独立里程碑）。
- 自定义主题编辑器（只提供 `theme.json` 的色板/密度/标题）。
- 生成路由、登录、多页应用（设计 §3 明确排除）。
- 分享页的问答入口（会放大账单，明确不做）。
- 分享页的任何写操作与开关。
