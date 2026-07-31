CREATE TABLE IF NOT EXISTS admin_jobs (
  id UUID PRIMARY KEY,
  shop_domain TEXT NOT NULL,
  action TEXT NOT NULL CHECK (action IN ('theme_audit', 'theme_canary', 'theme_sync')),
  status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
  actor_email TEXT NOT NULL,
  request JSONB NOT NULL DEFAULT '{}'::jsonb,
  result JSONB NOT NULL DEFAULT '{}'::jsonb,
  error TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  started_at TIMESTAMPTZ,
  finished_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS admin_jobs_shop_created_idx
  ON admin_jobs (shop_domain, created_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS admin_jobs_one_active_theme_idx
  ON admin_jobs (shop_domain)
  WHERE status IN ('queued', 'running');
