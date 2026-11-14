-- MCP / Skill 资源目录：工作流级绑定的可选项。
-- global 由平台维护（owner 为空）；private 归属某个用户，只能被本人引用。
CREATE TABLE IF NOT EXISTS resources (
  id            TEXT PRIMARY KEY,
  kind          TEXT NOT NULL CHECK (kind IN ('mcp','skill')),
  scope         TEXT NOT NULL CHECK (scope IN ('global','private')),
  owner_user_id TEXT,
  tenant_id     TEXT,
  ref           TEXT NOT NULL,
  name          TEXT NOT NULL,
  description   TEXT NOT NULL DEFAULT '',
  spec          JSONB NOT NULL DEFAULT '{}'::jsonb,
  enabled       BOOLEAN NOT NULL DEFAULT true,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (
    (scope = 'global' AND owner_user_id IS NULL)
    OR (scope = 'private' AND owner_user_id IS NOT NULL)
  )
);

-- 同名引用：全局唯一；私有按 owner 唯一（PG 的唯一约束不拦 NULL，用部分索引）
CREATE UNIQUE INDEX IF NOT EXISTS resources_global_ref
  ON resources (kind, ref) WHERE scope = 'global';
CREATE UNIQUE INDEX IF NOT EXISTS resources_private_ref
  ON resources (kind, ref, owner_user_id) WHERE scope = 'private';
