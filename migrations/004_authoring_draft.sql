-- 创作面 A 期：生成与发布分离，中间态是"可编辑草稿"。
-- stage 取值：chatting（只聊）→ drafting（有草稿待确认）→ publishing → published / failed
ALTER TABLE planner_sessions
  ADD COLUMN IF NOT EXISTS stage TEXT NOT NULL DEFAULT 'chatting';

ALTER TABLE planner_sessions
  DROP CONSTRAINT IF EXISTS planner_sessions_stage_check;

ALTER TABLE planner_sessions
  ADD CONSTRAINT planner_sessions_stage_check
  CHECK (stage IN ('chatting','drafting','publishing','published','failed'));

-- 最近一次发布/校验失败的原因：页面需要展示"为什么没发出去"
ALTER TABLE planner_sessions
  ADD COLUMN IF NOT EXISTS draft_issues JSONB NOT NULL DEFAULT '[]'::jsonb;
