export interface Env {
  NEON_DATABASE_URL: string;
  CF_ACCESS_TEAM_DOMAIN: string;
  CF_ACCESS_AUD: string;
  AWS_ACCESS_KEY_ID: string;
  AWS_SECRET_ACCESS_KEY: string;
  AWS_REGION: string;
  AWS_THEME_FUNCTION_NAME: string;
  AWS_CREDENTIAL_CREATED_AT: string;
  ALLOW_LOCAL_DEV?: string;
  SHOP_DOMAIN: string;
  SOURCE_LOCALE: string;
  APPROVED_THEME_ID: string;
  TARGET_LOCALES: string;
  CATALOG_POLL_SCHEDULE: string;
  SCHEDULED_SYNC_ENABLED: string;
  THEME_REALTIME_SYNC_ENABLED: string;
  OPENAI_MODEL?: string;
  OPENAI_FALLBACK_MODEL?: string;
  OPENAI_MONTHLY_BUDGET_USD?: string;
  REPORT_TIME_ZONE?: string;
  DEPLOYMENT_LOCKED: string;
}

export type AppBindings = {
  Bindings: Env;
  Variables: {
    actorEmail: string;
    correlationId: string;
  };
};

export interface LocaleStatus {
  locale: string;
  status: string;
  count: number;
  updated_at: string | null;
}

export interface ThemeSourceStatus {
  theme_id: string;
  resource_type: string;
  count: number;
  updated_at: string | null;
}

export interface RecentTranslation {
  product_gid: string;
  target_locale: string;
  status: string;
  model: string;
  updated_at: string;
}

export type AdminJobAction = "theme_audit" | "theme_canary" | "theme_sync";
export type AdminJobStatus = "queued" | "running" | "succeeded" | "failed";

export interface AdminJob {
  id: string;
  action: AdminJobAction;
  status: AdminJobStatus;
  actor_email: string;
  request: Record<string, unknown>;
  result: Record<string, unknown>;
  error: string | null;
  attempts: number;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  updated_at: string;
}

export interface ThemeChangeEvent {
  event_id: string;
  actual_theme_id: string;
  theme_name: string;
  theme_role: string;
  topic: string;
  status: string;
  details: Record<string, unknown>;
  created_at: string;
}

export interface UsageDaily {
  day: string;
  estimated_cost_usd: number;
  openai_calls: number;
  ai_events: number;
}

export interface UsageModel {
  model: string;
  calls: number;
  prompt_tokens: number;
  cached_prompt_tokens: number;
  cache_write_tokens: number;
  completion_tokens: number;
  reasoning_tokens: number;
}

export interface RecentUsageEvent {
  event_key: string;
  event_type: "product" | "theme" | "global_resource";
  entity_id: string;
  topic: string;
  source: string;
  status: string;
  openai_calls: number;
  estimated_cost_usd: number;
  estimate_complete: boolean;
  models: Record<string, Record<string, number>>;
  metadata: Record<string, unknown>;
  created_at: string;
}

export interface UsageOverview {
  today_usd: number;
  seven_days_usd: number;
  month_usd: number;
  thirty_days_usd: number;
  openai_calls_30d: number;
  fallback_calls_30d: number;
  ai_events_30d: number;
  no_ai_events_30d: number;
  product_ai_events_30d: number;
  avg_product_usd_30d: number;
  prompt_tokens_30d: number;
  cached_prompt_tokens_30d: number;
  cache_write_tokens_30d: number;
  completion_tokens_30d: number;
  latest_event_at: string | null;
  estimates_complete: boolean;
  pricing_date: string | null;
  daily: UsageDaily[];
  models: UsageModel[];
  recent: RecentUsageEvent[];
}

export interface DashboardOverview {
  database_bytes: string;
  product_sources: number;
  product_source_updated_at: string | null;
  product_translations: LocaleStatus[];
  translation_memory: number;
  dictionary_entries: number;
  theme_sources: ThemeSourceStatus[];
  theme_translations: LocaleStatus[];
  recent_translations: RecentTranslation[];
  recent_theme_events: ThemeChangeEvent[];
  jobs: AdminJob[];
  usage: UsageOverview;
}
