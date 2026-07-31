CREATE TABLE IF NOT EXISTS admin_content_edits (
  id uuid PRIMARY KEY,
  shop_domain text NOT NULL,
  actor_email text NOT NULL,
  entity_type text NOT NULL CHECK (entity_type IN ('product', 'theme')),
  entity_id text NOT NULL,
  resource_id text NOT NULL,
  field_key text NOT NULL,
  locale text NOT NULL CHECK (locale IN ('de', 'fr')),
  source_digest text NOT NULL,
  value_sha256 text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS admin_content_edits_shop_created_idx
  ON admin_content_edits (shop_domain, created_at DESC);
