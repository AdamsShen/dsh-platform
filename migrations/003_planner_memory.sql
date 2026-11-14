-- P2-T11：创作会话的记忆（滚动摘要 + 结构化槽位）与文档附件
-- 只做增量，不改已有列语义，保证 001 建好的表在旧数据上直接升级。

ALTER TABLE planner_sessions
  ADD COLUMN IF NOT EXISTS summary TEXT NOT NULL DEFAULT '',
  ADD COLUMN IF NOT EXISTS summarized_upto BIGINT NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS slots JSONB NOT NULL DEFAULT '{}'::jsonb;

-- 生成编排（P2-T12）要按步产 artifact：payload 存包内文件相对路径与摘要，content 存给人看的文本
ALTER TABLE planner_messages
  ADD COLUMN IF NOT EXISTS payload JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE INDEX IF NOT EXISTS idx_planner_sessions_owner
  ON planner_sessions(tenant_id, user_id, created_at DESC);

-- 创作会话的 token 单独计科目 platform.authoring，不混进 run 的成本
ALTER TABLE llm_usage DROP CONSTRAINT IF EXISTS llm_usage_purpose_check;
ALTER TABLE llm_usage ADD CONSTRAINT llm_usage_purpose_check
  CHECK (purpose IN ('plan', 'resolve', 'node', 'observe', 'authoring'));
