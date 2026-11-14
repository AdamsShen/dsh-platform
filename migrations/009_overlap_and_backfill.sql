-- P2-T19：重叠策略扩展 + 手动 backfill。
-- 策略对齐 Temporal ScheduleOverlapPolicy 的一个子集：
--   skip        上一次未结束就跳过（P2-T17 的既有行为，保持默认）
--   buffer_one  缓冲一个槽位，当前 run 结束后再拉起；缓冲已满则丢弃新的
--   cancel_other 置旧 run 的 cancel_requested（节点边界退出），随后启动新的
-- 不提供 terminate_other：本项目停止只在节点边界生效，强杀与设计约束冲突。

ALTER TABLE triggers ADD COLUMN IF NOT EXISTS overlap_policy TEXT NOT NULL DEFAULT 'skip';

-- 缓冲槽位：调度器无状态，缓冲必须落库，否则多副本/重启就丢
ALTER TABLE triggers ADD COLUMN IF NOT EXISTS pending_fire_at TIMESTAMPTZ;
-- 入缓冲的时间，用于兜底过期（run 卡死时避免缓冲永久挂住）
ALTER TABLE triggers ADD COLUMN IF NOT EXISTS pending_since TIMESTAMPTZ;

ALTER TABLE triggers DROP CONSTRAINT IF EXISTS triggers_overlap_policy_check;
ALTER TABLE triggers ADD CONSTRAINT triggers_overlap_policy_check
  CHECK (overlap_policy IN ('skip','buffer_one','cancel_other'));

CREATE INDEX IF NOT EXISTS triggers_pending
  ON triggers (pending_fire_at) WHERE enabled AND pending_fire_at IS NOT NULL;

-- 审计新增两种结局：buffered（进缓冲）、cancelled（因 cancel_other 被取消的旧 run）
ALTER TABLE trigger_events DROP CONSTRAINT IF EXISTS trigger_events_status_check;
ALTER TABLE trigger_events ADD CONSTRAINT trigger_events_status_check
  CHECK (status IN ('pending','consumed','failed','skipped','buffered','cancelled'));
