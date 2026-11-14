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

## 三、未完成：整条链路还没接线

代码侧发现：`issue_key` / `revoke_key` / `spend_logs` / `sync_spend_logs` **在 `src/` 里没有任何调用点**，`current_gateway()` 也从未被调用。目前 `runs.spent_usd` 完全来自 `usage.py` 的本地价目表估算（`llm_usage.model` 标 `mock-1`），网关适配器是死代码。

要真正以网关账单为准，需要补三块：

1. **执行注入**：`_dsh_runtime.py` 已支持 `api_key` / `base_url`（`/Library/git-root/dsh-platform/src/ao/harness/_dsh_runtime.py:779`，取自 `settings.dsh_base_url`），但现在是全局配置。要按 `(run, node, attempt)` 签发并逐次注入，需把这两个值从 `settings` 提升为调用参数（影响 `harness/base.py` 协议签名）。
2. **归因透传**：每次模型调用带 `x-litellm-spend-logs-metadata`（`run_id/node_id/attempt/purpose`）。这是结论 6 直接带来的简化——不必为归因建签发表。
3. **定时同步**：scheduler 里周期性 `sync_spend_logs` 写 `llm_usage`，再刷新 `runs.spent_usd`；按 `gateway_request_id` 去重（结论 7 的延迟要求晚到补写）。
4. **可选：签发表**：只在需要"事后按 key 追查某次尝试"或做 key 生命周期审计时才建；归因本身已不需要它。结论 3 说明 alias 不能复用，重签要加唯一后缀（时间戳或 attempt）。

风险点：注入虚拟 key 后，DSH 的所有模型调用都会打网关，`AO_GATEWAY_URL` 不可用时会 **跑不动**（当前是本地估算、可用）。所以接线必须带失败策略（网关不可用时降级还是 fail closed），这属于要拍板的设计取舍，先不擅自动。

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
