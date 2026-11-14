# 模型调用网关：业界方案与开源选型

- 日期：2026-11-14
- 范围：13 项（8 个可自托管开源 + 2 个商业托管 + 2 组平台对标 + 1 组计费组件），时间窗 2025-11 至今
- 产出方式：Deep Research 流程；本机无 `web-search-agent`，Step 2/3 用并行 `web_search` + `web_fetch` 完成，逐项结果在 `results/*.json`（含 `uncertain` 标记）
- 目的：回答本项目的实际问题——「按 run/节点/尝试次数做成本归因与硬预算」，业界是上网关还是编排层自己算；上网关的话选哪个

## 一、结论先行

1. **业界是两套东西叠着用，不是二选一。** 网关负责「谁能用哪个模型、花超了立刻拒、每次调用留一条可核对的流水」；编排层负责「这条流水属于哪个 run、哪个节点、第几次尝试」。没有哪一个网关能替你解决 run 归因，也没有哪个编排框架能替你做服务端硬限。
2. **开源里只有 LiteLLM 和 Bifrost 同时具备「虚拟 key + 服务端硬预算 + 逐条 spend 流水」这三件套。** Portkey 开源偏数据面（治理与预算在商业版）；Envoy/Higress/APISIX/Kong 是「API 网关加 AI 插件」，只有 token 限流，没有 key 账户与流水表。
3. **主流编排平台（LangGraph、Temporal）都不做硬预算**，只在图状态里累加 token 并做节点边界检查，即「软硬限」。Dify 的粒度更粗（租户/应用），没有 run 概念。这反过来说明：想要硬预算，绕不开模型出口那一层。
4. **对本项目的建议**：如果目标只是内部成本可见 + 超了能停，编排层自己记账足够，不必上网关；如果目标是「一个 run 花费绝不能超上限」或未来对外计费，选 LiteLLM，并接受它对 PG + Redis 的运行时依赖。Bifrost 是备选（单二进制、低开销），但自定义 metadata 归因未经实测，不能直接押注。

## 二、对比总表

| 方案 | 许可证 | 虚拟 key | 服务端硬预算 | 自定义 metadata 归因 | 逐条 spend 流水 API | 部署依赖 | 主要风险 |
|---|---|---|---|---|---|---|---|
| **LiteLLM Proxy** | MIT 核心 | ✅ 完整（签发/吊销/轮换/过期） | ✅ 超预算 402 | ✅ `spend_logs_metadata`（实测可行） | ✅ `/spend/logs/v2` 分页 | PG 必需 + Redis（多副本） | breaking change 多；Python 服务较重 |
| **Bifrost** | Apache-2.0 | ✅ | ✅ 分层（key/team/customer） | ⚠️ 未见说明 | ⚠️ 有 logging 插件，明细 API 未确认 | 单二进制，默认文件存储 | 归因能力未验证；benchmark 自家发布且数字自相矛盾 |
| **Portkey Gateway** | **MIT**（不是 Apache-2.0） | ⚠️ 声称支持，治理在商业版 | ⚠️ 主要在托管/企业版 | ⚠️ 未说明 | ⚠️ 开源版仅本地控制台 | 单 Node 服务，无强制 DB | 关键治理能力要付费；2.0 预发布期边界在变 |
| **Envoy AI Gateway** | Apache-2.0 | ❌ | ⚠️ 仅 token 限流 | ⚠️ 只能靠指标打标 | ❌ 无流水表 | K8s + Envoy + Redis | 运维重；1.0 新发布 |
| **Higress** | Apache-2.0 | ❌（Consumer/key-auth） | ⚠️ token 限流 | ⚠️ consumer/header 维度 | ❌ | K8s + Envoy + Redis | 计量弱于 LiteLLM |
| **APISIX AI Gateway** | Apache-2.0 | ❌（Consumer） | ⚠️ token 限流 | ⚠️ consumer/route | ❌ | etcd + Redis | 通用网关，AI 能力靠插件拼 |
| **Kong AI Gateway** | 开源核心 + 商业 | ❌ | ⚠️ 关键插件在商业版 | ⚠️ | ❌ | PG 或 DB-less + Redis | 等价能力基本要靠 Konnect |
| **Helicone** | Apache-2.0 | ❌ | ⚠️ 弱 | ✅ `Helicone-Property-*` | ✅ 有查询 API | ClickHouse + PG | **2026-03 被收购，已进维护模式** |
| **OpenRouter** | 专有 SaaS | ⚠️ Management API 按客户签发 key | ✅ 账户预算 + 单 key 限额 | ⚠️ 按 key 近似 | ✅ per-key 报表 | 无 | 数据出境；无法自建备路 |
| **Cloudflare AI Gateway** | 专有 SaaS | ❌ | ✅ spend limits（超限 429） | ⚠️ 支持 metadata 维度 | ⚠️ Dashboard + Logpush | 无 | 绑定 Cloudflare；日志按量计费 |
| **LangGraph / Temporal** | 开源 + 商业 | ❌ | ❌（只能软限） | 不适用（自身就有 run/attempt 上下文） | ❌（自己建表） | 自身持久化 | 需要自己写记账与预算判断 |
| **Dify** | Apache-2.0 + 商业 | ❌ | ⚠️ 商业版额度池 | ❌ | ❌ | PG + Redis + 向量库 | 粒度是应用/租户，非 run |
| **Lago / OpenMeter** | AGPL-3.0 / 开源 | ❌ | ⚠️ entitlement 可限但需网关配合 | ✅ 自定义事件属性 | ✅ 计费 API | OpenMeter：Kafka+ClickHouse | 只做「记账→出账」，内部平台用属过度设计 |

## 三、几个决定成败的细节

### 1. 「虚拟 key」不是重点，「流水能归因」才是
很多人以为虚拟 key 的用途是隔离凭据，其实对本平台真正的价值是**把花费切到一次尝试上**。但归因有两条路，代价差很多：

- **每 attempt 一个 key，再按 `api_key` 反查**：要维护签发表，而且 LiteLLM 的 `key_alias` **全局唯一且删除后仍占位**，重试重签会直接 400，必须加唯一后缀。实测结论 3。
- **一个 key 复用到多个 attempt，靠请求头透传归因**：`x-litellm-spend-logs-metadata` 会落到 `SpendLogs.metadata.spend_logs_metadata`，实测可取回 `run_id/node_id/attempt/purpose`，**不需要建签发表**。

后者是更省事的路子。注意三个坑（都来自实测）：
- 归因块在 `metadata.spend_logs_metadata`，**不是** `metadata` 顶层，判断层级写错会得到「归因为空」的错误结论。
- **签发 key 时写的 metadata 不会进流水**，只有请求级的才进。
- `metadata.tags` 落到 `request_tags`，但同 key 的记录会累积，不能当请求级归因键用。

### 2. `/spend/logs` 的默认值是聚合，不是明细
`/spend/logs` 默认 `summarize=true`，返回的是按天汇总（`{startTime, spend, users, models}`），没有 `request_id`。要明细必须显式 `summarize=false`（且截断最近 1 万行），或改走 `/spend/logs/v2` 分页。**只看默认响应会得出「网关不提供明细」的错误结论。**

### 3. 被拒请求不一定进流水
key 或 model 解析阶段之前就拒掉的请求不写 SpendLogs，要看 `GET /gateway/daily/activity`。这意味着「用 spend log 反推调用次数」在异常场景下会对不上，做对账时要知道这一点。

### 4. 延迟开销要按量级看，不要信营销数字
Bifrost 宣称 11 µs（5000 RPS、t3.xlarge），同页 About 又写 “under 200µs”，相差一个数量级；Helicone 是 5-15 ms（代理模式）；Envoy/APISIX 量级在亚毫秒到数毫秒。对 LLM 调用（几百毫秒到几十秒）而言，这些差异通常不是选型主因，**运维复杂度才是**。

### 5. 「开源」的含金量差别很大
- LiteLLM：MIT 核心，虚拟 key + 预算 + 流水都在开源里（本项目实测确认）。
- Portkey：**MIT 不是 Apache-2.0**（网上文章有误传），但预算/治理/RBAC/语义缓存多数归托管版，开源版主要是路由与 guardrails 数据面。
- Kong：token 限流的 advanced 插件、语义缓存、用量计费都在 Konnect。
- Helicone：Apache-2.0 可自托管，但 2026-03 被 Mintlify 收购后进入维护模式，官方建议迁出（tracing 转 Langfuse 一类）。

## 四、对本项目的选型建议

现状：只有一个真实 provider（DSH/DeepSeek），单租户内部使用，`spent_usd` 由 `usage.py` 按固定价目表估算，网关适配器尚未接线。

| 目标 | 推荐 | 代价 |
|---|---|---|
| 只要「花得看得见 + 超了能停」 | **不上网关**：把 `usage.py` 的单一价目表改成按 model 查表，保留节点边界软预算（现状 + 改动极小） | 接受硬限滞后一次调用；无 key 级审计 |
| 「一个 run 绝不能超预算」或未来对外计费 | **LiteLLM**：签 key 逐 run 注入 + 请求头透传归因 + 定时同步 spend log | 运行时强依赖 PG/Redis；需要定网关故障策略（fail closed vs 直连降级） |
| 想要硬预算但拒绝 Python 服务 | **Bifrost**（备选）：单二进制、Apache-2.0、开销最低 | 必须先实测「自定义 metadata 能否归因」，否则只能按 key 归因 |

**明确不建议**：Envoy AI Gateway / Higress / APISIX / Kong（除非本平台已经跑在 K8s 服务网格上，否则为了计量引入一整套数据面不划算）；Helicone（维护模式）；Cloudflare / OpenRouter（数据出境与合规要先过）。

## 五、来源

- LiteLLM：《Spend Tracking》 https://docs.litellm.ai/docs/proxy/cost_tracking ；《Virtual Keys》 https://docs.litellm.ai/docs/proxy/virtual_keys ；第三人称成本归因实践 https://www.tokenfind.cn/blog/ai-api-cost-attribution-tracking-2026
- Portkey：仓库 https://github.com/Portkey-AI/gateway ；对比 https://api7.ai/ja/portkey-vs-litellm
- Bifrost：仓库 https://github.com/maximhq/bifrost ；厂商对比 https://www.getmaxim.ai/articles/top-5-open-source-llm-gateways-compared-2026/
- Envoy AI Gateway：https://aigateway.envoyproxy.io/docs/next/capabilities/traffic/usage-based-ratelimiting/ ；1.0 发布 https://tetrate.io/blog/envoy-ai-gateway-v1-0-release
- Higress：https://higress.ai/en/ai-gateway ；中文横评 https://www.apiseven.com/ai-gateway-comparison
- APISIX：https://apisix.apache.org/zh/ ；插件细节 https://blog.gitcode.com/5f8d6930713c529280c9f8b23a98284c.html ；Geekflare 评测 https://geekflare.com/open-source/apisix-ai-gateway/
- Kong：https://dev.to/sahajmeet_kaur_/ai-gateway-comparison-2026-kong-portkey-litellm-and-truefoundry-mf1 ；https://www.getmaxim.ai/articles/5-tools-for-rate-limiting-llm-apis-at-scale/
- Helicone：收购与维护模式 https://jacar.es/?p=4581 ；迁出建议 https://panoply.io/?p=22672
- OpenRouter：Management API 按客户签发 key https://openrouter.zendesk.com/hc/en-us/articles/51680687417499 ；团队花费治理 https://openrouter.ai/blog/insights/governing-team-ai-spend/
- Cloudflare：定价 https://developers.cloudflare.com/ai-gateway/reference/pricing/ ；成本拆解 https://www.toolbistro.com/radar/cloudflare-ai-gateway-pricing
- Dify：Quota Management https://deepwiki.com/langgenius/dify/6.3-plugin-system-and-daemon ；多租户网关设计 https://repiw.com/multi-tenant-llm-gateway-dify-vpc-architecture
- LangGraph/Temporal：https://runcycles.io/blog/langgraph-budget-control-durable-execution-retries-fan-out ；http://theneuralbase.com/langgraph/learn/advanced/cost-control-token-budgets-per-graph-run
- 计费组件：https://devtoollab.com/blog/best-usage-based-billing-platforms ；https://founden.ai/articles/set-up-metered-billing-for-your-ai-product-2026
- 可观测标准：https://opentelemetry.io/blog/2026/genai-observability ；https://llmcfo.com/research/opentelemetry-genai-cost-tracking
- 本项目实测：`docs/reports/2026-11-14-gateway-probe.md`（探针 `scripts/probe_gateway.py`，14/14）
