-- 014_usage_user_backfill：把历史账单补上用户维度（P5-T06）
-- 012 已加 llm_usage.user_id 与索引，这里只回填存量行。
-- 口径：run 的 trigger_user_id 就是业务主体（扇出时由触发器写入）；
--       创作会话（run_id 为空）不归属用户，保持 NULL。

UPDATE llm_usage u
   SET user_id = r.trigger_user_id
  FROM runs r
 WHERE r.id = u.run_id
   AND u.user_id IS NULL
   AND r.trigger_user_id IS NOT NULL;
