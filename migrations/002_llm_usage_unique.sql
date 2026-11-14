-- 002：网关流水按 request_id 去重；本地估算（mock）没有 request_id，允许为空
CREATE UNIQUE INDEX IF NOT EXISTS uq_llm_usage_gateway_request
  ON llm_usage (gateway_request_id)
  WHERE gateway_request_id IS NOT NULL;
