-- P1-T01 差异 3：外部副作用幂等日志（effect journal）。
--
-- 背景：LangGraph 的恢复粒度是节点边界（见 docs/research/2026-11-13-execution-kernel.md §1），
-- 节点内崩溃后整个节点重跑，action 会被再调一次。这张表让同一副作用只真执行一次。
--
-- 键不含 attempt：跨 attempt 复用才叫幂等；参数变了 hash 就变，自然成为另一个副作用。
-- owner_epoch 用来判断"上一任 owner 是否已失权"，比时钟可靠（迟到的旧 owner 一定 epoch 更小）。

CREATE TABLE IF NOT EXISTS run_effects (
  run_id      TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  node_id     TEXT NOT NULL,
  action      TEXT NOT NULL,
  params_hash TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'started'
                CHECK (status IN ('started','succeeded','failed')),
  -- 最后一次认领它的 executor epoch；用于接管判断，不参与唯一键
  owner_epoch INTEGER NOT NULL,
  -- 被认领过几次（含接管），排查重复调用用
  attempt     INTEGER NOT NULL DEFAULT 0,
  result      JSONB,
  error       TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (run_id, node_id, action, params_hash)
);

-- 详情页按 run 倒序列副作用
CREATE INDEX IF NOT EXISTS idx_run_effects_run ON run_effects (run_id, created_at DESC);
