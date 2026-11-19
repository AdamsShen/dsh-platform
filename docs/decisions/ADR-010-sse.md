# ADR-010 实时推送：SSE + run_events 轮询（未实现）

- 状态：**已决策，未实现**（P3-T05 延后，见 `docs/plans/2026-11-19-remaining-work-plan.md` §7）
- 日期：2026-11-13 决策 / 2026-11-19 标记现状
- 相关：P3-T05；`src/ao/web/app.py`、`run_events` 表

## 背景

MVP 详情页用 3 秒 meta refresh，刷新会丢状态、无法续传，且页面越多对主库压力越大。P3 要求"详情页 1 秒内看到 `node.started`"、"断开重连不丢事件"。

## 决策（尚未落地，代码零实现）

1. 用 **SSE**（`sse-starlette`），不引入 WebSocket——推送是单向的，SSE 复用现有 HTTP/反向代理。
2. 事件源是 `run_events` 表**轮询**（500ms 一批），不依赖 PG `LISTEN/NOTIFY`；NOTIFY 只作为可选优化。
3. 断线续传：客户端带 `Last-Event-ID`，服务端先补发该 id 之后的事件，再增量推送；15s 心跳保活。
4. 路由规划：`GET /api/runs/{id}/events/stream`。
5. 每页一个连接，离开即断开；服务端按 run 分组合并轮询，避免 N 个页面 N 条查询。

## 理由

- 事件表已有 append-only 语义与单调 id，天然适合 `Last-Event-ID` 续传；轮询实现不需要长连接事务与 NOTIFY 权限。
- 500ms 轮询对本项目事件量（单 run 数十条）成本可忽略；必要时再加 NOTIFY，接口不变。
- 不用 WebSocket：无双向需求，反而多一套握手与代理配置。

## 替代方案

| 方案 | 否决原因 |
|---|---|
| 继续 meta refresh | 丢事件、无法续传、无法做到 1s 内可见 |
| WebSocket | 双向能力用不上，代理与鉴权成本更高 |
| 直接 PG LISTEN/NOTIFY 作为主路径 | 需要长连接与额外权限，断线后仍需回表补齐，等于两套逻辑 |

## 后果

- 正向：接口与前端契约先定死，后续换传输方式不影响页面。
- 负向：当前仍为 3s refresh，P3-T05 未开工前不得宣称"实时"；压测（P3-T10）也需要 SSE 先落地才能测连接数。
- 实现时注意：SSE 连接不占执行池槽位；鉴权与 RBAC 过滤必须在建流时做一次，不能只靠前端隐藏。

## 验证（实现时）

- `tests/e2e/test_sse.py`：断开重连不丢事件；详情页 1s 内收到 `node.started`。
