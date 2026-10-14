CREATE TABLE IF NOT EXISTS translation_usage_events (
  shop_domain TEXT NOT NULL,
  event_key TEXT NOT NULL,
  event_type TEXT NOT NULL CHECK (event_type IN ('product', 'theme', 'global_resource')),
  entity_id TEXT NOT NULL DEFAULT '',
  topic TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'unknown',
  openai_calls INTEGER NOT NULL DEFAULT 0 CHECK (openai_calls >= 0),
  fallback_calls INTEGER NOT NULL DEFAULT 0 CHECK (fallback_calls >= 0),
  cache_hits INTEGER NOT NULL DEFAULT 0 CHECK (cache_hits >= 0),
  cache_misses INTEGER NOT NULL DEFAULT 0 CHECK (cache_misses >= 0),
  prompt_tokens BIGINT NOT NULL DEFAULT 0 CHECK (prompt_tokens >= 0),
  cached_prompt_tokens BIGINT NOT NULL DEFAULT 0 CHECK (cached_prompt_tokens >= 0),
  cache_write_tokens BIGINT NOT NULL DEFAULT 0 CHECK (cache_write_tokens >= 0),
  completion_tokens BIGINT NOT NULL DEFAULT 0 CHECK (completion_tokens >= 0),
  reasoning_tokens BIGINT NOT NULL DEFAULT 0 CHECK (reasoning_tokens >= 0),
  duration_ms BIGINT NOT NULL DEFAULT 0 CHECK (duration_ms >= 0),
  estimated_cost_usd NUMERIC(16, 8) NOT NULL DEFAULT 0 CHECK (estimated_cost_usd >= 0),
  pricing_date DATE,
  estimate_complete BOOLEAN NOT NULL DEFAULT TRUE,
  models JSONB NOT NULL DEFAULT '{}'::jsonb,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (shop_domain, event_key)
);

CREATE INDEX IF NOT EXISTS translation_usage_events_shop_created_idx
  ON translation_usage_events (shop_domain, created_at DESC);

CREATE INDEX IF NOT EXISTS translation_usage_events_shop_entity_idx
  ON translation_usage_events (shop_domain, event_type, entity_id, created_at DESC);
