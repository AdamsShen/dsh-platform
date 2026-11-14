-- 001_init：执行内核的全量表（设计 §15 + 租约/心跳/step）
-- 约定：金额 numeric(14,6)，时间 timestamptz，结构化数据 jsonb。

CREATE TABLE IF NOT EXISTS tenants (
  id                TEXT PRIMARY KEY,
  name              TEXT NOT NULL,
  quota_daily_usd   NUMERIC(14,6) NOT NULL DEFAULT 0,
  quota_daily_tokens BIGINT NOT NULL DEFAULT 0,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS users (
  id            TEXT PRIMARY KEY,
  tenant_id     TEXT NOT NULL REFERENCES tenants(id),
  username      TEXT NOT NULL UNIQUE,
  display_name  TEXT NOT NULL,
  role          TEXT NOT NULL CHECK (role IN ('operator','admin')),
  status        TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('pending','active','rejected')),
  password_hash TEXT NOT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS api_keys (
  id           TEXT PRIMARY KEY,
  user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name         TEXT NOT NULL,
  prefix       TEXT NOT NULL,
  key_hash     TEXT NOT NULL UNIQUE,
  scopes       JSONB NOT NULL DEFAULT '[]'::jsonb,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_used_at TIMESTAMPTZ,
  revoked_at   TIMESTAMPTZ,
  expires_at   TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS workflow_defs (
  id         TEXT PRIMARY KEY,
  tenant_id  TEXT NOT NULL REFERENCES tenants(id),
  parent_id  TEXT REFERENCES workflow_defs(id),
  name       TEXT NOT NULL,
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS workflow_versions (
  id            TEXT PRIMARY KEY,
  workflow_id   TEXT NOT NULL REFERENCES workflow_defs(id) ON DELETE CASCADE,
  version       INTEGER NOT NULL,
  manifest_hash TEXT NOT NULL,
  package_ref   TEXT NOT NULL,
  compiled_ref  TEXT,
  smoke_ref     TEXT,
  status        TEXT NOT NULL DEFAULT 'draft'
                CHECK (status IN ('draft','published','rejected')),
  published_at  TIMESTAMPTZ,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (workflow_id, version)
);

CREATE TABLE IF NOT EXISTS planner_sessions (
  id          TEXT PRIMARY KEY,
  tenant_id   TEXT NOT NULL,
  user_id     TEXT NOT NULL,
  workflow_id TEXT,
  status      TEXT NOT NULL DEFAULT 'active',
  memory      JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS planner_messages (
  id          BIGSERIAL PRIMARY KEY,
  session_id  TEXT NOT NULL REFERENCES planner_sessions(id) ON DELETE CASCADE,
  role        TEXT NOT NULL,
  content     TEXT NOT NULL DEFAULT '',
  attachments JSONB NOT NULL DEFAULT '[]'::jsonb,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_planner_messages_session
  ON planner_messages(session_id, id);

CREATE TABLE IF NOT EXISTS runs (
  id               TEXT PRIMARY KEY,
  tenant_id        TEXT NOT NULL,
  workflow_id      TEXT NOT NULL REFERENCES workflow_defs(id),
  version          INTEGER NOT NULL,
  package_ref      TEXT NOT NULL,
  dedupe_key       TEXT NOT NULL UNIQUE,
  subject          JSONB NOT NULL DEFAULT '{}'::jsonb,
  trigger_user_id  TEXT,
  status           TEXT NOT NULL CHECK (status IN (
                     'queued','running','succeeded','failed','cancelled',
                     'paused_budget','waiting_hitl')),
  current_node     TEXT,
  epoch            INTEGER NOT NULL DEFAULT 0,
  lease_owner      TEXT,
  lease_until      TIMESTAMPTZ,
  cancel_requested BOOLEAN NOT NULL DEFAULT false,
  budget_usd       NUMERIC(14,6) NOT NULL DEFAULT 0,
  spent_usd        NUMERIC(14,6) NOT NULL DEFAULT 0,
  retry_count      INTEGER NOT NULL DEFAULT 0,
  retry_at         TIMESTAMPTZ,
  queue_reason     TEXT,
  error            TEXT,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at       TIMESTAMPTZ,
  ended_at         TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_runs_status_retry ON runs(status, retry_at);
CREATE INDEX IF NOT EXISTS idx_runs_lease ON runs(lease_until);
CREATE INDEX IF NOT EXISTS idx_runs_tenant ON runs(tenant_id, created_at);

CREATE TABLE IF NOT EXISTS run_nodes (
  run_id       TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  node_id      TEXT NOT NULL,
  attempt      INTEGER NOT NULL DEFAULT 0,
  kind         TEXT NOT NULL DEFAULT 'agent',
  status       TEXT NOT NULL,
  native_id    TEXT,
  host         TEXT,
  phase        TEXT,
  output       JSONB,
  output_ref   TEXT,
  cost         NUMERIC(14,6) NOT NULL DEFAULT 0,
  repair_count INTEGER NOT NULL DEFAULT 0,
  meta         JSONB NOT NULL DEFAULT '{}'::jsonb,
  error        TEXT,
  started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  ended_at     TIMESTAMPTZ,
  PRIMARY KEY (run_id, node_id, attempt)
);

-- 节点内 step：图状态答"走到哪"，DSH 答"说到哪句"，两者只在 native_id 对齐
CREATE TABLE IF NOT EXISTS run_steps (
  run_id     TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  node_id    TEXT NOT NULL,
  attempt    INTEGER NOT NULL,
  step       INTEGER NOT NULL,
  native_id  TEXT,
  host       TEXT,
  phase      TEXT NOT NULL,
  state      JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (run_id, node_id, attempt, step)
);

CREATE TABLE IF NOT EXISTS run_vars (
  run_id      TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  node_id     TEXT NOT NULL DEFAULT '',
  name        TEXT NOT NULL,
  value       JSONB,
  source      TEXT NOT NULL DEFAULT 'mcp',
  resolved_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (run_id, node_id, name)
);

CREATE TABLE IF NOT EXISTS run_events (
  seq        BIGSERIAL PRIMARY KEY,
  run_id     TEXT NOT NULL,
  node_id    TEXT,
  type       TEXT NOT NULL,
  payload    JSONB NOT NULL DEFAULT '{}'::jsonb,
  actor      TEXT NOT NULL DEFAULT 'system',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_run_events_run ON run_events(run_id, seq);

CREATE TABLE IF NOT EXISTS llm_usage (
  id                 BIGSERIAL PRIMARY KEY,
  run_id             TEXT,
  node_id            TEXT,
  attempt            INTEGER,
  purpose            TEXT NOT NULL DEFAULT 'node'
                     CHECK (purpose IN ('plan','resolve','node','observe')),
  model              TEXT NOT NULL,
  input_tokens       BIGINT NOT NULL DEFAULT 0,
  cached_tokens      BIGINT NOT NULL DEFAULT 0,
  output_tokens      BIGINT NOT NULL DEFAULT 0,
  cost               NUMERIC(14,6) NOT NULL DEFAULT 0,
  currency           TEXT NOT NULL DEFAULT 'USD',
  gateway_request_id TEXT,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_run ON llm_usage(run_id);
CREATE INDEX IF NOT EXISTS idx_llm_usage_tenant_time ON llm_usage(created_at);

CREATE TABLE IF NOT EXISTS bindings (
  id         TEXT PRIMARY KEY,
  scope      TEXT NOT NULL CHECK (scope IN ('global','user','run')),
  scope_id   TEXT NOT NULL,
  kind       TEXT NOT NULL CHECK (kind IN ('mcp','skill')),
  name       TEXT NOT NULL,
  config     JSONB NOT NULL DEFAULT '{}'::jsonb,
  readonly   BOOLEAN NOT NULL DEFAULT true,
  created_by TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (scope, scope_id, kind, name)
);

CREATE TABLE IF NOT EXISTS worker_heartbeats (
  worker_id TEXT PRIMARY KEY,
  host      TEXT NOT NULL,
  pool      TEXT NOT NULL DEFAULT 'execution',
  slots     INTEGER NOT NULL DEFAULT 1,
  last_seen TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS trigger_events (
  id          TEXT PRIMARY KEY,
  workflow_id TEXT NOT NULL,
  trigger_id  TEXT NOT NULL,
  event_key   TEXT NOT NULL UNIQUE,
  payload     JSONB NOT NULL DEFAULT '{}'::jsonb,
  status      TEXT NOT NULL DEFAULT 'pending'
              CHECK (status IN ('pending','consumed','failed')),
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS share_links (
  id          TEXT PRIMARY KEY,
  tenant_id   TEXT NOT NULL,
  workflow_id TEXT NOT NULL,
  version     INTEGER NOT NULL,
  page_id     TEXT NOT NULL,
  scope       JSONB NOT NULL,
  token_hash  TEXT NOT NULL UNIQUE,
  expires_at  TIMESTAMPTZ NOT NULL,
  revoked_at  TIMESTAMPTZ,
  max_uses    INTEGER,
  used_count  INTEGER NOT NULL DEFAULT 0,
  created_by  TEXT NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_share_links_token ON share_links(token_hash);

CREATE TABLE IF NOT EXISTS share_access (
  id         BIGSERIAL PRIMARY KEY,
  share_id   TEXT NOT NULL REFERENCES share_links(id) ON DELETE CASCADE,
  page_id    TEXT NOT NULL,
  ip         TEXT,
  ua         TEXT,
  status     INTEGER NOT NULL,
  in_scope   BOOLEAN NOT NULL DEFAULT true,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_share_access_share ON share_access(share_id, created_at);
