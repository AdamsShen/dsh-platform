-- 011：虚拟 key 登记表，把网关流水按 api_key(=token) 反查回归因字段。
--
-- 为什么需要这张表：LiteLLM 的请求级归因在 `metadata.spend_logs_metadata`，
-- 但 DSH 走 SDK 子进程，无法保证透传自定义请求头（2026-11-14 实测 harness 配置
-- 只有 base_url/api_key，没有 headers）。所以归因改为「每次调用签一个虚拟 key，
-- 用 SpendLogs.api_key（= 签发返回的 token）反查 run/node/attempt」。
--
-- 注意 key_alias 全局唯一且删除后仍占位（见 docs/reports/2026-11-14-gateway-probe.md 事实 3），
-- 所以每次签发都要带唯一后缀，不能靠固定 alias 做幂等。

CREATE TABLE IF NOT EXISTS gateway_keys (
  key_token   TEXT PRIMARY KEY,
  key_alias   TEXT NOT NULL,
  run_id      TEXT NOT NULL,
  node_id     TEXT NOT NULL,
  attempt     INTEGER NOT NULL,
  purpose     TEXT NOT NULL DEFAULT 'node',
  budget_usd  NUMERIC(14,6) NOT NULL DEFAULT 0,
  expires_at  TIMESTAMPTZ,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  revoked_at  TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_gateway_keys_run ON gateway_keys(run_id);
CREATE INDEX IF NOT EXISTS idx_gateway_keys_created ON gateway_keys(created_at);
