-- 012_fanout：多用户与定时扇出（P5-T01 / T02 / T06 的列）
-- 职责：回答"一条计划要展开给哪些主体""每个主体一次跑多少预算"；
--       不回答"主体名单怎么维护"（mcp_query 放 B 期，本期只做目录与静态名单）。
-- 约定：扇出配置挂在 triggers 上（一条工作流的展开方式不随 run 变化），
--       费用维度的 user_id 冗余到 llm_usage，避免报表每次都 JOIN JSONB。

-- 引擎侧主体目录：users 表 001 就建了，但全仓无读写；这里补业务主键，
-- 避免把平台内部 id 当业务 uid 写进产物与计费。
ALTER TABLE users ADD COLUMN IF NOT EXISTS external_ref TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS users_external_ref
  ON users (tenant_id, external_ref) WHERE external_ref IS NOT NULL;

-- 扇出模式：none 保持现状（一次到点一个 run）
ALTER TABLE triggers ADD COLUMN IF NOT EXISTS fanout_mode TEXT NOT NULL DEFAULT 'none';
ALTER TABLE triggers ADD COLUMN IF NOT EXISTS fanout_spec JSONB NOT NULL DEFAULT '{}'::jsonb;
-- 单次展开上限：超出直接拒绝整批，不做截断（截断会让业务方以为全跑完了）
ALTER TABLE triggers ADD COLUMN IF NOT EXISTS fanout_limit INTEGER NOT NULL DEFAULT 1000;
-- 单主体预算：透到 runs.budget_usd，由网关侧硬限兜住
ALTER TABLE triggers ADD COLUMN IF NOT EXISTS budget_per_run NUMERIC(14,6) NOT NULL DEFAULT 0;

ALTER TABLE triggers DROP CONSTRAINT IF EXISTS triggers_fanout_mode_check;
ALTER TABLE triggers ADD CONSTRAINT triggers_fanout_mode_check
  CHECK (fanout_mode IN ('none', 'tenant_active', 'static_list'));

ALTER TABLE triggers DROP CONSTRAINT IF EXISTS triggers_fanout_limit_check;
ALTER TABLE triggers ADD CONSTRAINT triggers_fanout_limit_check
  CHECK (fanout_limit BETWEEN 1 AND 5000);

-- 用户维度费用（P5-T06 的列先落，查询在 W30 接）
ALTER TABLE llm_usage ADD COLUMN IF NOT EXISTS user_id TEXT;
CREATE INDEX IF NOT EXISTS idx_llm_usage_user ON llm_usage (user_id, created_at);

-- 按主体查在跑的 run：扇出后重叠判定与排障都走这条
CREATE INDEX IF NOT EXISTS idx_runs_subject_user
  ON runs ((subject->>'user_id')) WHERE subject ? 'user_id';
