-- P2-T18：事件型触发 + 错过时刻回填。
-- 事件触发没有"下次时刻"，所以要放开 next_fire_at 的 NOT NULL；
-- 回填窗口/上限给保守默认值：停机一小时内最多补一次，避免一次性打爆队列。

ALTER TABLE triggers ALTER COLUMN next_fire_at DROP NOT NULL;
-- 事件型没有 cron 表达式（006 里 cron 是 NOT NULL）
ALTER TABLE triggers ALTER COLUMN cron DROP NOT NULL;

ALTER TABLE triggers ADD COLUMN IF NOT EXISTS event_name TEXT;
ALTER TABLE triggers ADD COLUMN IF NOT EXISTS catchup_window_seconds INTEGER NOT NULL DEFAULT 3600;
ALTER TABLE triggers ADD COLUMN IF NOT EXISTS catchup_limit INTEGER NOT NULL DEFAULT 1;

-- 006 的匿名 CHECK 约束名是 PG 自动生成的 triggers_type_check
ALTER TABLE triggers DROP CONSTRAINT IF EXISTS triggers_type_check;
ALTER TABLE triggers ADD CONSTRAINT triggers_type_check CHECK (type IN ('cron','event'));

ALTER TABLE triggers DROP CONSTRAINT IF EXISTS triggers_shape_check;
ALTER TABLE triggers ADD CONSTRAINT triggers_shape_check CHECK (
  (type <> 'cron' OR next_fire_at IS NOT NULL)
  AND (type <> 'event' OR event_name IS NOT NULL)
);

-- 入站事件按 (工作流, 事件名) 找启用的计划
CREATE INDEX IF NOT EXISTS triggers_event_lookup
  ON triggers (workflow_id, event_name) WHERE enabled;
