# 创作面对话页 UI 调研（P2-T15）

日期：2026-11-13。范围：对话式"说需求 → 生成工作流"的界面形态与服务端实现选型。

## 一、业内做法

| 方案 | 形态 | 值得借鉴 | 不适用的地方 |
|---|---|---|---|
| LangGraph Studio / LangSmith | 画布为主，代码优先；调试与 trace 强 | 步骤/状态可视化、逐节点重跑 | 面向开发者，客户不会用；且本项目产物是包不是图编辑器 |
| n8n / Dify / Coze 的 AI Builder | 对话入口 + 画布确认，双栏并存 | **对话负责采集与澄清，结构化预览负责确认**；缺槽位就地追问 | 画布拖拽编辑器成本极高，本期不做 |
| Workflow Builder（LangGraph 之上的客户侧编辑器） | 对话 + 面向客户的表单/图视图 | 客户侧只暴露"确认/微调"，不暴露节点内部 | 需要成熟的图 schema，本项目 manifest 已够用但要先做只读预览 |

共性结论（3 个以上方案一致）：

1. **纯聊天不可见状态**：用户不知道系统记住了什么、还缺什么，容易反复说同一句。主流解法是把槽位/节点清单作为独立区域展示。
2. **纯画布门槛高**：面向业务人员的产品都把"建图"降级为"确认清单"，图只用于展示。
3. **生成是异步长任务**：普遍先返回任务、再逐步刷新进度，而不是一个请求等到天荒地老。

来源：
- https://www.workflowbuilder.io/blog/best-visual-editors-for-langgraph
- https://jimmysong.io/en/blog/open-source-ai-agent-workflow-comparison/
- https://b1key.com/en/blog/visual-canvas-beats-chat-for-complex-ai-agent-work-81af
- https://arxiv.org/pdf/2506.20091v1（人机协作式多 Agent 系统的 HCI 挑战）

## 二、服务端实现选型

主流是 **FastAPI + Jinja2 + HTMX，必要时叠加 SSE**，不引入 SPA 构建链：

- FastAPI + Jinja2 + HTMX 模板：`brgsstm/ai-chat-template`（SSE 流式 + 服务端渲染），省掉前端工具链。
- SSE 的坑在代理缓冲：需要 `X-Accel-Buffering: no`，否则浏览器会在结束时一次性收到全部内容（`dev.to` 一文专门讲这个）。
- 中文实践同样以"Jinja2 模板 + StaticFiles + SSE 路由"为主。

来源：
- http://onlybits.org/brgsstm/ai-chat-template
- https://dev.to/amankumar_apiclaw/streaming-llm-output-to-the-browser-through-your-own-backend-fastapi-server-sent-events-and-the-buffering-traps
- https://www.hqwc.cn/a/902709.html

## 三、本项目拟采用

### 形态：单页双栏

- 左栏：对话流（消息 + 上传附件 + 输入框）。
- 右栏：**方案预览**——槽位卡（缺失项高亮）、节点清单、七步进度（读 `role='artifact'` 的 payload）、发布结果（workflow_id / package_ref）。
- 生成中只读，不提供图编辑器；"改需求"仍然走对话，符合"对话是真值、产物是结果"的既有约定。

### 技术：模板 + HTMX 轮询，SSE 留作增强

`orchestrator.generate` 是长任务（真实模型单轮 10s 级别，加冒烟更久）。做法：

1. `POST /planner/{sid}/generate` 起后台 task，立即返回"已开始"；
2. 页面用 HTMX `hx-trigger="every 1s"` 拉 `GET /planner/{sid}/steps`，逐步渲染 artifact；
3. 任务结束后停轮询，展示发布结果。

先轮询后 SSE 的理由：SSE 需要处理反代缓冲与断线重连，而生成进度本身就是 1s 粒度，轮询足够；等有真实部署环境再升级 SSE。

### 路由与文件清单

| 路由 | 作用 |
|---|---|
| `GET /` | 重定向到最近会话，无会话则新建 |
| `POST /planner/sessions` | 建会话并跳转 |
| `GET /planner/{sid}` | 对话页（消息 + 槽位 + 步骤 + 结果） |
| `POST /planner/{sid}/messages` | 追加用户消息，返回消息片段（HTMX 局部替换） |
| `POST /planner/{sid}/uploads` | 上传文档，返回消息片段 + 刷新槽位卡 |
| `POST /planner/{sid}/generate` | 启动生成 task |
| `GET /planner/{sid}/steps` | 步骤进度片段（轮询目标） |

文件：

- `src/ao/web/__init__.py`
- `src/ao/web/app.py`（app 工厂 + 上述路由；当前无鉴权，tenant/user 从 `AO_DEFAULT_TENANT` 与 `X-User-Id` 头取，缺省 `u_001`）
- `src/ao/web/templates/base.html` / `planner.html` / `_messages.html` / `_steps.html` / `_slots.html`
- `scripts/serve.py`（uvicorn 启动入口）
- `tests/e2e/test_web_planner.py`（httpx ASGI：建会话 → 发消息 → 上传 → 触发生成 → 轮询到 published → 页面含节点标题）

### 备选

- **Streamlit/Gradio**：出界面最快，但样式不可控、无法嵌到平台控制台，且流式与轮询都要另写，弃。
- **React SPA**：交互上限高，但当前仓库无前端构建链，本期引入成本不划算，留到控制面成型后再说。

## 四、风险与边界

- 鉴权/多租户：本期只做接口预留（`X-User-Id`），不做登录。
- 并发生成：同一会话重复点"生成"需要互斥（DB 行锁或内存字典），本期用会话级 asyncio.Lock。
- 生成失败：页面展示 `issues`（代码 + 文案 + 是否用尽回退轮数），不隐藏原因。

## 五、需求补充后的设计（2026-11-13 追加）

补充需求：节点不拖拽但**可编辑**；节点可绑定 **MCP / Skill（全局或私有）**；工作流可设**触发时机（定时等）**。

### 5.1 关键结构变更：生成与发布必须分开

现在 `orchestrator.generate()` 是"生成完立刻发布"，没有可编辑的中间态。要支持节点编辑，必须先落草稿：

1. `prepare()`：第 1-4 步（澄清 → 清单 → 包 → graph），产出**草稿**，会话进入 `draft` 阶段，页面渲染可编辑的节点卡片；
2. `update_draft()`：改节点标题/职责/outputs/提示词（**绑定不在这里**）→ 重写包文件、重生成 graph.py；
3. `publish_draft()`：第 5-7 步（静态检查 → 冒烟 → 发布），失败仍走 3 轮修复循环（修复的是"清单"，不是人手改的内容——人改过的优先，修复只在人未改时启用，避免覆盖手工编辑）。

会话状态机：`chatting → drafting → publishing → published | failed`，落在 `planner_sessions.stage`，页面按阶段切换右栏形态。

**取舍**：编辑只作用于草稿，已发布版本不可变（要改就新开版本）。这与"版本不可变 + 内容寻址"的既有约束一致。

### 5.2 MCP / Skill：工作流级绑定

绑定属于**整个工作流**，不属于某个节点。节点只描述"做什么"，"能用哪些工具"是工作流的运行环境。

manifest 顶层新增：

```json
{
  "bindings": [
    {"kind": "mcp", "ref": "documents", "scope": "global"},
    {"kind": "skill", "ref": "weekly-report", "scope": "private", "owner": "u_001"}
  ]
}
```

配套事实（决定了改造量）：

- `Node.mcp/.skills` 字段还在（`manifest.py:46`），但**链路其实没接线**：`executor.call_agent` 收下 `mcp/skills` 参数后直接 `return await self._turn(...)`，参数没往下传（`executor.py:331`）；`RunEnv` 虽然定义了 `mcp/skills` 字段（`wf_sdk/runtime.py:28`），构造时也没赋值（`executor.py:584`）。
- provider 侧要的是 `context["mcp_servers"]` 字典（`harness/dsh.py:88`），由 `_dsh_runtime._mcp_patch` 转成 MCP 客户端补丁。

因此改造分两步，且第一次真正打通：

1. **数据**：`Manifest.bindings` 新增；`Node.mcp/.skills` 保留解析但标记废弃，生成器不再写。加载老包时把节点级引用**并集提升**为工作流级绑定，行为不变。
2. **执行**：`executor.py:584` 构造 `RunEnv` 时按 `bindings` 分组填入 `mcp/skills`；`_turn` 解析出 `mcp_servers` 放进 provider context。所有 agent 节点共享同一套绑定，节点不再各自声明。

`resources` 目录表（B 期）：

| 字段 | 说明 |
|---|---|
| `id` / `kind` | `mcp` 或 `skill` |
| `scope` | `global`（平台内置）或 `private`（owner 私有） |
| `owner_user_id` | private 时的归属人；global 为空 |
| `tenant_id` | 预留：后续支持租户共享 |
| `name` / `spec` | MCP 存 server 配置，Skill 存名称与描述 |
| `enabled` | 下架不等于删除，历史版本仍可运行 |

绑定存引用名，不存明文密钥；发布期校验"引用存在 + 有权使用"（私有资源只有 owner 能用），防止越权。UI 上绑定区挂在**工作流设置面板**，节点卡片里不出现 MCP/Skill。

### 5.3 触发器

`Trigger(type, cron, timezone, fanout)` 模型已在 `manifest.py:64`，但只写进包，**没有任何调度代码**（`scheduler.py` 现在只做租约回收 + 失败重试）。

做法：发布时把 triggers 投影到 `workflow_triggers`（`workflow_id, version, trigger_id, type, cron, timezone, enabled, next_fire_at`），调度器每 30s 扫 `next_fire_at <= now()`：

1. 用 `croniter`（纯 Python）算下次时间，`SELECT ... FOR UPDATE SKIP LOCKED` 防多实例重复；
2. 按 `dedupe_key = f"{workflow_id}:{version}:{trigger_id}:{scheduled_at}"` 插 `trigger_events`（表已存在，`event_key` 唯一），重复触发自然去重；
3. 事件转成 run 入队，走既有租约队列。

先支持 `manual` + `cron`；事件触发（webhook / 文件到达）留 `trigger_events.payload` 接口，不先做。

### 5.4 分期与文件清单

| 期 | 内容 | 文件 |
|---|---|---|
| A | 对话页骨架 + 草稿可编辑 + 确认发布 | `migrations/004_authoring_draft.sql`（`planner_sessions.stage`）、`web/app.py`、5 个模板、`orchestrator.prepare/update_draft/publish_draft`、`tests/e2e/test_web_planner.py` |
| B | 资源目录与绑定 | `migrations/005_resources.sql`、`ao/resources.py`、模板中的多选绑定、发布期归属校验 |
| C | 定时触发 | `migrations/006_workflow_triggers.sql`、`ao/triggers.py`、`scheduler` 扫描、`tests/integration/test_triggers.py`（注入时钟，不 sleep） |

依赖：`croniter`（C 期）。A 期不引入新依赖。

### 5.5 待确认

- 私有资源归属按"个人私有"（`owner_user_id`）实现，租户共享只留字段不做 UI——符合你说的"全局 + 私有"。
- 定时触发最小粒度 1 分钟（cron 标准），不支持秒级。
- 编辑阶段允许改 outputs 字段；改字段会连带要求下游 `when` 表达式仍合法，发布前统一校验并给出具体报错。

