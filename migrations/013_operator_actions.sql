-- 013_operator_actions：运维写操作留痕（P3-T08，M2-W22）
-- 职责：回答"谁在什么时候对哪个对象做了什么、成没成"；
--       不回答"run 内部发生了什么"（那是 run_events）。
-- 约定：只 append，不更新、不删除；越权尝试也落一行（result='denied'）。

CREATE TABLE IF NOT EXISTS operator_actions (
  id          TEXT PRIMARY KEY,
  actor       TEXT NOT NULL,
  action      TEXT NOT NULL,
  object_type TEXT NOT NULL,
  object_id   TEXT,
  scope       TEXT,
  scope_id    TEXT,
  result      TEXT NOT NULL DEFAULT 'ok'
              CHECK (result IN ('ok', 'denied', 'failed')),
  detail      TEXT NOT NULL DEFAULT '',
  payload     JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS operator_actions_created
  ON operator_actions (created_at DESC);
CREATE INDEX IF NOT EXISTS operator_actions_actor
  ON operator_actions (actor, created_at DESC);
CREATE INDEX IF NOT EXISTS operator_actions_object
  ON operator_actions (object_type, object_id, created_at DESC);
