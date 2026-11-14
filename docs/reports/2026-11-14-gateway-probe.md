# LiteLLM 网关契约验证（P1-T12 前置）

- 日期：2026-11-14
- 目的：把 `src/ao/gateway.py` 依赖的 HTTP 契约拿真实实例钉住，而不是照文档猜字段
- 环境：`ghcr.io/berriai/litellm:main-latest`（2026-11-14 拉取）+ `postgres:16`，本地 4000 端口，模型用 `mock_response` 免真实上游
- 复跑：`AO_GATEWAY_URL=... AO_GATEWAY_MASTER_KEY=... AO_GATEWAY_PROBE_MODEL=mock-gpt uv run python scripts/probe_gateway.py`（14 项检查，只对测试实例跑，会真实建/删虚拟 key）

## 一、实测结论

| # | 契约点 | 实测结果 | 影响 |
|---|---|---|---|
| 1 | `/key/generate` 的 key 标识 | 有 `key`（sk-…）、`token`（64 位 hash）、`key_name`（`sk-...d4jw` 脱敏展示名）；**没有** `key_id` | 原代码 `key_name or token` 取到展示名，无法归因 → 已改为优先 `token` |
| 2 | `duration: "1d"` | 生效，`expires` 为签发时刻 +1 天（响应里 `duration` 回显 `null`，不要据此判断） | 无需改；注意判断过期只能看 `expires` |
| 3 | `key_alias` 唯一性 | 全局唯一，且删除后仍占位：同 alias 再次签发返回 **400** "already exists" | 固定 alias（`run:node:attempt:purpose`）不能承担重签幂等，重试/接管会直接失败 |
| 4 | `/spend/logs` | **默认 `summarize=true`** 返回按天聚合（`{startTime, spend, users, models}`，无 `request_id`）；显式传 `summarize=false` 才返回逐条明细，但结果截断到最近 10000 行 | 原 `normalize_spend` 拿不到 `run_id`，`sync_spend_logs` 会丢弃**全部**行 → 已改走 v2 分页，不受截断影响 |
| 5 | `/spend/logs/v2` | 逐条明细，`{data, page, page_size, total_pages, total_is_capped}`；行内含 `request_id`、`api_key`（= 虚拟 key 的 `token`）、`model`、`spend`、`prompt_tokens`、`completion_tokens`、`startTime`；`YYYY-MM-DD` 与 `YYYY-MM-DD HH:MM:SS` 两种时间格式都接受 | 已改为走 v2 并翻页；`total` 在 1 万处会被截断（`total_is_capped`），翻页要以 `data` 为空为准 |
| 6 | 请求级 metadata | **会落库**，位置是 `metadata.spend_logs_metadata`（比 `metadata` 深一层）：请求体 `metadata.spend_logs_metadata` 或 header `x-litellm-spend-logs-metadata` 都生效；`metadata.tags` 进 `request_tags`（同 key 的记录会累积，不能当请求级归因用）；**签发 key 时写的 metadata 不会进流水** | run 维度可以靠透传 header 解决，不必依赖签发表 |
| 7 | spend 落库延迟 | 调用后约 **2-4s** 才能查到流水 | 同步任务要容忍延迟，不能"调完立刻结算" |
| 8 | `/key/delete` | 可用；删除后该 key 调模型立即 **401** | 吊销语义可用，不必依赖过期时间兜底 |

> 修正说明：本报告 2026-11-14 初版曾判定"请求级 metadata 不落库"与"`/spend/logs` 已退化为聚合"，两者都是错的。前者是因为只在 `metadata` 顶层找 `run_id`，没看到嵌套的 `spend_logs_metadata`；后者是因为漏看 `summarize` 参数默认值。已按官方文档 + 复测更正，探针也从 13/14 变为 14/14。

## 二、本次已修

`/Library/git-root/dsh-platform/src/ao/gateway.py`

- `issue_key`：`key_id` 改取 `token`，并回填 `expires_at`（原来恒为空串）
- `spend_logs`：改打 `/spend/logs/v2` 并按 `total_pages` 翻页，返回逐条明细
- `normalize_spend`：从 `metadata.spend_logs_metadata` 读归因字段，并透出 `key_token`（= `api_key`）作为兜底

测试：`tests/unit/test_gateway_contract.py` 5 条（token 优先、翻页、错误码、嵌套归因、`key_token` 兜底）。

探针结果：**14/14 通过**。

## 三、整条链路已接线（2026-11-14）

原来 `issue_key` / `revoke_key` / `spend_logs` / `sync_spend_logs` 在 `src/` 里没有调用点，是死代码；现已按第二档接完，`AO_GATEWAY_ENABLED=1` 时生效（默认关，保持本地估算行为）。

1. **执行注入**：`executor._turn` 每 turn 调 `issue_key`，`max_budget` 取 run 剩余预算；`api_key` / `gateway_base_url` 经 provider `context` 透到 `_dsh_runtime._run_once` 的 `base_kwargs`，turn 结束 `finally` 吊销。预算超限由网关返回 402 → `BudgetExceeded` → `paused_budget`。
2. **归因：改走签发表而非请求头**。原计划用 `x-litellm-spend-logs-metadata`，但 DSH 的 `DeepSeekHarnessConfig` 只有 `base_url`/`api_key`（`env` 只影响子进程环境），**没有自定义请求头入口**。所以改为「每 turn 签虚拟 key → `SpendLogs.api_key`（= `token`）反查 `migrations/011_gateway_keys.sql`」。`normalize_spend` 仍优先读 `metadata.spend_logs_metadata`，缺失时才反查。
3. **定时同步**：`scheduler.sync_gateway_spend()` 按 `AO_GATEWAY_SYNC_INTERVAL`（默认 60s）拉「昨天到今天」（流水 2-4s 延迟 + 跨天边界），`sync_spend_logs` 按 `gateway_request_id` 去重并回刷 `runs.spent_usd`。
4. **失败策略**：`AO_GATEWAY_FAIL_OPEN` 默认 0 = fail closed（网关不可用就报错，不产生无归因账单）；置 1 则直连上游并落 `gateway.degraded` 事件。
5. **alias**：`key_alias` 加 `uuid4` 短后缀，规避「别名全局唯一且删除后仍占位」（事实 3）。

单测：`tests/unit/test_gateway_sync.py` 6 条（token 反查、header 优先、重复同步幂等、无归因丢弃、`base_url` 派生），全量 134 passed。

### 端到端实测（2026-11-14，真实 DeepSeek 上游）

起真实 LiteLLM（`model=deepseek/deepseek-chat` 转发 `api.deepseek.com`），DSH 真实对话跑通：

```
[1] key=sk-PzaI5PRFY... token=06cbac822fb2...
[2] DSH: ok 'ok' meta={'finish_reason': 'completed'}
[3] 同步新增: 1
[4] llm_usage: node_id=fetch, model=deepseek/deepseek-chat,
    cost=0.000206, gateway_request_id=fc2b9243-...
```

过程中又钉住四个新事实（都已写进代码）：

1. **DSH 走 Anthropic Messages 协议**：`POST /v1/messages`，`base_url` 必须是**根**（`http://gw:4000`），带 `/v1` 会拼成 `/v1/v1/messages`。所以 `gateway_root()` 与 `gateway_openai_base()` 分开，executor 注入根地址。
2. **超预算返回 422 而不是 402**：`{"error":{"type":"budget_exceeded","code":"422","message":"Budget has been exceeded!..."}}`。必须按 message/type 识别，否则会当普通失败进重试循环。已在 `_dsh_runtime._is_budget_error` 映射为 `status="budget_exceeded"` → `paused_budget`。
3. **`end_date` 按当天 00:00 解析**：只传今天会把今天的流水全部排除（实测 `end=2026-11-14` 返回 0 行，`end=2026-11-15` 返回 1 行）。同步窗口固定为「昨天 → 明天」。
4. **mock_response 模型不产流**：即使 `stream:true` 也返回完整 JSON，DSH 会报 `Messages stream ended before message_stop`。所以验证 DSH 链路必须用真实上游，不能用 mock 模型。

**仍未验证**：共享 Redis 多副本吊销；「被拒请求不写 SpendLogs」的对账语义；多副本下 `gateway_keys` 的并发写入（单机足够）。

## 四、遗留

- 共享 Redis：多副本下吊销/限流需要，单机验证覆盖不到
- "被拒请求（402）不写 SpendLogs" 的对账语义：需在接真实上游后复核

## 五、如何复现环境

`config.yaml`（mock 模型，不需要真实上游 key）：

```yaml
model_list:
  - model_name: mock-gpt
    litellm_params:
      model: gpt-4o-mini
      api_key: sk-fake-for-local-test
      mock_response: "hello from mock"
```

```bash
docker network create ao-litellm-net
docker run -d --name ao-litellm-pg --network ao-litellm-net \
  -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=testpass -e POSTGRES_DB=litellm postgres:16
docker run -d --name ao-litellm --network ao-litellm-net -p 4000:4000 \
  -e LITELLM_MASTER_KEY=sk-master-test \
  -e DATABASE_URL=postgresql://postgres:testpass@ao-litellm-pg:5432/litellm \
  -v "$PWD/config.yaml:/app/config.yaml" \
  ghcr.io/berriai/litellm:main-latest --config /app/config.yaml
```

启动约 40s（内含 prisma 迁移），`curl localhost:4000/health/liveliness` 返回 `"I'm alive!"` 即可跑探针。
