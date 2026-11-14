-- 006 的唯一索引是 (workflow_id) WHERE enabled，只允许一个工作流有一条生效计划。
-- P2-T18 之后一个工作流可以同时挂 cron 和 event 计划（"每天 9 点" + "订单创建时"），
-- 因此把唯一键放宽到 (工作流, 类型, cron, 事件名)：同类同参数的重复计划仍被拦住。
DROP INDEX IF EXISTS triggers_active_per_workflow;

CREATE UNIQUE INDEX IF NOT EXISTS triggers_active_plan ON triggers (
  workflow_id,
  type,
  COALESCE(cron, ''),
  COALESCE(event_name, '')
) WHERE enabled;
