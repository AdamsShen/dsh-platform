-- 定时触发器投影：把已发布版本里的 manifest.triggers 展开成可扫描的行。
-- 为什么单独一张表：版本是包 + JSONB 的产物，"到点"要能一条索引查询搞定，
-- 不能每次都去解析所有 manifest。
CREATE TABLE IF NOT EXISTS triggers (
  id            TEXT PRIMARY KEY,
  workflow_id   TEXT NOT NULL REFERENCES workflow_defs(id) ON DELETE CASCADE,
  version       INTEGER NOT NULL,
  tenant_id     TEXT NOT NULL,
  -- 触发身份：run.trigger_user_id 用它，未来做配额/审计
  owner_user_id TEXT,
  type          TEXT NOT NULL DEFAULT 'cron' CHECK (type IN ('cron','event')),
  cron          TEXT NOT NULL,
  timezone      TEXT NOT NULL DEFAULT 'Asia/Shanghai',
  -- 冗余存包引用，触发时不用回查 workflow_versions
  package_ref   TEXT NOT NULL,
  enabled       BOOLEAN NOT NULL DEFAULT true,
  next_fire_at  TIMESTAMPTZ NOT NULL,
  last_fired_at TIMESTAMPTZ,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 一个工作流同时只有一条生效计划：发布即切换，旧计划 disable
CREATE UNIQUE INDEX IF NOT EXISTS triggers_active_per_workflow
  ON triggers (workflow_id) WHERE enabled;
CREATE INDEX IF NOT EXISTS triggers_due
  ON triggers (next_fire_at) WHERE enabled;

-- 审计表补一个"因重叠跳过"的状态（001 里只允许 pending/consumed/failed）
ALTER TABLE trigger_events DROP CONSTRAINT IF EXISTS trigger_events_status_check;
ALTER TABLE trigger_events ADD CONSTRAINT trigger_events_status_check
  CHECK (status IN ('pending','consumed','failed','skipped'));
