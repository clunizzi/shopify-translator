import { neon } from "@neondatabase/serverless";
import type { AdminJob, AdminJobAction, DashboardOverview, Env } from "./types";

type OverviewRow = {
  overview: DashboardOverview;
};

export class ActiveJobError extends Error {
  constructor() {
    super("A theme operation is already queued or running");
    this.name = "ActiveJobError";
  }
}

export async function loadOverview(env: Env): Promise<DashboardOverview> {
  if (!env.NEON_DATABASE_URL) {
    throw new Error("NEON_DATABASE_URL is not configured");
  }

  const sql = neon(env.NEON_DATABASE_URL);
  const reportTimeZone = String(env.REPORT_TIME_ZONE || "UTC").trim() || "UTC";
  const rows = await sql`
    WITH
    database_usage AS (
      SELECT pg_database_size(current_database())::text AS bytes
    ),
    product_source AS (
      SELECT
        COUNT(*)::int AS count,
        MAX(updated_at)::text AS updated_at
      FROM pdp_source_state
      WHERE shop_domain = ${env.SHOP_DOMAIN}
        AND source_locale = ${env.SOURCE_LOCALE}
        AND section_hashes <> '{}'::jsonb
    ),
    product_translation_groups AS (
      SELECT
        translation.target_locale AS locale,
        translation.status,
        COUNT(*)::int AS count,
        MAX(translation.updated_at)::text AS updated_at
      FROM pdp_translation_state AS translation
      JOIN pdp_source_state AS source
        ON source.shop_domain = translation.shop_domain
       AND source.product_gid = translation.product_gid
       AND source.source_locale = ${env.SOURCE_LOCALE}
       AND source.section_hashes <> '{}'::jsonb
      WHERE translation.shop_domain = ${env.SHOP_DOMAIN}
      GROUP BY translation.target_locale, translation.status
      ORDER BY translation.target_locale, translation.status
    ),
    memory_usage AS (
      SELECT COUNT(*)::int AS count
      FROM translation_memory
    ),
    dictionary_usage AS (
      SELECT COUNT(*)::int AS count
      FROM translation_dictionary
    ),
    theme_source_groups AS (
      SELECT
        theme_id,
        resource_type,
        COUNT(*)::int AS count,
        MAX(updated_at)::text AS updated_at
      FROM theme_source_state
      WHERE shop_domain = ${env.SHOP_DOMAIN}
      GROUP BY theme_id, resource_type
      ORDER BY theme_id, resource_type
    ),
    theme_translation_groups AS (
      SELECT
        target_locale AS locale,
        status,
        COUNT(*)::int AS count,
        MAX(updated_at)::text AS updated_at
      FROM theme_translation_state
      WHERE shop_domain = ${env.SHOP_DOMAIN}
        AND theme_id = ${env.APPROVED_THEME_ID}
      GROUP BY target_locale, status
      ORDER BY target_locale, status
    ),
    recent_product_translations AS (
      SELECT
        translation.product_gid,
        translation.target_locale,
        translation.status,
        translation.model,
        translation.updated_at::text
      FROM pdp_translation_state AS translation
      JOIN pdp_source_state AS source
        ON source.shop_domain = translation.shop_domain
       AND source.product_gid = translation.product_gid
       AND source.source_locale = ${env.SOURCE_LOCALE}
       AND source.section_hashes <> '{}'::jsonb
      WHERE translation.shop_domain = ${env.SHOP_DOMAIN}
      ORDER BY translation.updated_at DESC
      LIMIT 12
    ),
    recent_theme_events AS (
      SELECT
        event_id,
        actual_theme_id,
        theme_name,
        theme_role,
        topic,
        status,
        details,
        created_at::text
      FROM theme_change_events
      WHERE shop_domain = ${env.SHOP_DOMAIN}
      ORDER BY created_at DESC
      LIMIT 8
    ),
    recent_jobs AS (
      SELECT
        id::text,
        action,
        status,
        actor_email,
        request,
        result,
        error,
        attempts,
        created_at::text,
        started_at::text,
        finished_at::text,
        updated_at::text
      FROM admin_jobs
      WHERE shop_domain = ${env.SHOP_DOMAIN}
      ORDER BY created_at DESC
      LIMIT 12
    ),
    usage_bounds AS (
      SELECT
        (
          (NOW() AT TIME ZONE ${reportTimeZone})::date::timestamp
          AT TIME ZONE ${reportTimeZone}
        ) AS today_start,
        (
          date_trunc('month', NOW() AT TIME ZONE ${reportTimeZone})
          AT TIME ZONE ${reportTimeZone}
        ) AS month_start
    ),
    usage_window AS (
      SELECT *
      FROM translation_usage_events
      WHERE shop_domain = ${env.SHOP_DOMAIN}
        AND created_at >= (
          SELECT today_start - INTERVAL '29 days' FROM usage_bounds
        )
    ),
    usage_totals AS (
      SELECT
        COALESCE(SUM(estimated_cost_usd) FILTER (
          WHERE created_at >= (SELECT today_start FROM usage_bounds)
        ), 0)::float8 AS today_usd,
        COALESCE(SUM(estimated_cost_usd) FILTER (
          WHERE created_at >= (
            SELECT today_start - INTERVAL '6 days' FROM usage_bounds
          )
        ), 0)::float8 AS seven_days_usd,
        COALESCE(SUM(estimated_cost_usd) FILTER (
          WHERE created_at >= (SELECT month_start FROM usage_bounds)
        ), 0)::float8 AS month_usd,
        COALESCE(SUM(estimated_cost_usd), 0)::float8 AS thirty_days_usd,
        COALESCE(SUM(openai_calls), 0)::int AS openai_calls_30d,
        COALESCE(SUM(fallback_calls), 0)::int AS fallback_calls_30d,
        COUNT(*) FILTER (WHERE openai_calls > 0)::int AS ai_events_30d,
        COUNT(*) FILTER (WHERE openai_calls = 0)::int AS no_ai_events_30d,
        COUNT(*) FILTER (
          WHERE event_type = 'product' AND openai_calls > 0
        )::int AS product_ai_events_30d,
        COALESCE(
          AVG(estimated_cost_usd) FILTER (
            WHERE event_type = 'product' AND openai_calls > 0
          ),
          0
        )::float8 AS avg_product_usd_30d,
        COALESCE(SUM(prompt_tokens), 0)::bigint AS prompt_tokens_30d,
        COALESCE(SUM(cached_prompt_tokens), 0)::bigint AS cached_prompt_tokens_30d,
        COALESCE(SUM(cache_write_tokens), 0)::bigint AS cache_write_tokens_30d,
        COALESCE(SUM(completion_tokens), 0)::bigint AS completion_tokens_30d,
        MAX(created_at)::text AS latest_event_at,
        COALESCE(BOOL_AND(estimate_complete) FILTER (WHERE openai_calls > 0), TRUE)
          AS estimates_complete,
        MAX(pricing_date)::text AS pricing_date
      FROM usage_window
    ),
    usage_daily AS (
      SELECT
        (created_at AT TIME ZONE ${reportTimeZone})::date::text AS day,
        SUM(estimated_cost_usd)::float8 AS estimated_cost_usd,
        SUM(openai_calls)::int AS openai_calls,
        COUNT(*) FILTER (WHERE openai_calls > 0)::int AS ai_events
      FROM usage_window
      GROUP BY 1
      ORDER BY 1
    ),
    usage_models AS (
      SELECT
        model.key AS model,
        COALESCE(SUM((model.value ->> 'calls')::bigint), 0)::int AS calls,
        COALESCE(SUM((model.value ->> 'prompt_tokens')::bigint), 0)::bigint
          AS prompt_tokens,
        COALESCE(SUM((model.value ->> 'cached_prompt_tokens')::bigint), 0)::bigint
          AS cached_prompt_tokens,
        COALESCE(SUM((model.value ->> 'cache_write_tokens')::bigint), 0)::bigint
          AS cache_write_tokens,
        COALESCE(SUM((model.value ->> 'completion_tokens')::bigint), 0)::bigint
          AS completion_tokens,
        COALESCE(SUM((model.value ->> 'reasoning_tokens')::bigint), 0)::bigint
          AS reasoning_tokens
      FROM usage_window AS usage
      CROSS JOIN LATERAL jsonb_each(usage.models) AS model(key, value)
      GROUP BY model.key
      ORDER BY calls DESC, model.key
    ),
    recent_usage AS (
      SELECT
        event_key,
        event_type,
        entity_id,
        topic,
        source,
        status,
        openai_calls,
        estimated_cost_usd::float8 AS estimated_cost_usd,
        estimate_complete,
        models,
        metadata,
        created_at::text
      FROM translation_usage_events
      WHERE shop_domain = ${env.SHOP_DOMAIN}
      ORDER BY created_at DESC
      LIMIT 16
    )
    SELECT jsonb_build_object(
      'database_bytes', (SELECT bytes FROM database_usage),
      'product_sources', COALESCE((SELECT count FROM product_source), 0),
      'product_source_updated_at', (SELECT updated_at FROM product_source),
      'product_translations', COALESCE(
        (SELECT jsonb_agg(to_jsonb(product_translation_groups)) FROM product_translation_groups),
        '[]'::jsonb
      ),
      'translation_memory', COALESCE((SELECT count FROM memory_usage), 0),
      'dictionary_entries', COALESCE((SELECT count FROM dictionary_usage), 0),
      'theme_sources', COALESCE(
        (SELECT jsonb_agg(to_jsonb(theme_source_groups)) FROM theme_source_groups),
        '[]'::jsonb
      ),
      'theme_translations', COALESCE(
        (SELECT jsonb_agg(to_jsonb(theme_translation_groups)) FROM theme_translation_groups),
        '[]'::jsonb
      ),
      'recent_translations', COALESCE(
        (SELECT jsonb_agg(to_jsonb(recent_product_translations)) FROM recent_product_translations),
        '[]'::jsonb
      ),
      'recent_theme_events', COALESCE(
        (SELECT jsonb_agg(to_jsonb(recent_theme_events)) FROM recent_theme_events),
        '[]'::jsonb
      ),
      'jobs', COALESCE(
        (SELECT jsonb_agg(to_jsonb(recent_jobs)) FROM recent_jobs),
        '[]'::jsonb
      ),
      'usage', (
        SELECT to_jsonb(usage_totals) || jsonb_build_object(
          'daily', COALESCE(
            (SELECT jsonb_agg(to_jsonb(usage_daily)) FROM usage_daily),
            '[]'::jsonb
          ),
          'models', COALESCE(
            (SELECT jsonb_agg(to_jsonb(usage_models)) FROM usage_models),
            '[]'::jsonb
          ),
          'recent', COALESCE(
            (SELECT jsonb_agg(to_jsonb(recent_usage)) FROM recent_usage),
            '[]'::jsonb
          )
        )
        FROM usage_totals
      )
    ) AS overview
  `;

  const row = rows[0] as OverviewRow | undefined;
  if (!row?.overview) {
    throw new Error("Neon returned an empty overview");
  }
  return row.overview;
}

export async function createAdminJob(
  env: Env,
  input: {
    id: string;
    action: AdminJobAction;
    actorEmail: string;
    request: Record<string, unknown>;
  },
): Promise<AdminJob> {
  if (!env.NEON_DATABASE_URL) {
    throw new Error("NEON_DATABASE_URL is not configured");
  }
  const sql = neon(env.NEON_DATABASE_URL);

  await sql`
    UPDATE admin_jobs
    SET
      status = 'failed',
      error = 'Operation expired before completion',
      finished_at = NOW(),
      updated_at = NOW()
    WHERE shop_domain = ${env.SHOP_DOMAIN}
      AND status IN ('queued', 'running')
      AND updated_at < NOW() - INTERVAL '15 minutes'
  `;
  await sql`
    DELETE FROM admin_jobs
    WHERE shop_domain = ${env.SHOP_DOMAIN}
      AND created_at < NOW() - INTERVAL '30 days'
  `;

  try {
    const rows = await sql`
      INSERT INTO admin_jobs (
        id,
        shop_domain,
        action,
        status,
        actor_email,
        request
      )
      VALUES (
        ${input.id}::uuid,
        ${env.SHOP_DOMAIN},
        ${input.action},
        'queued',
        ${input.actorEmail},
        ${JSON.stringify(input.request)}::jsonb
      )
      RETURNING
        id::text,
        action,
        status,
        actor_email,
        request,
        result,
        error,
        attempts,
        created_at::text,
        started_at::text,
        finished_at::text,
        updated_at::text
    `;
    return rows[0] as AdminJob;
  } catch (error) {
    if (
      typeof error === "object" &&
      error !== null &&
      "code" in error &&
      error.code === "23505"
    ) {
      throw new ActiveJobError();
    }
    throw error;
  }
}

export async function failAdminJobInvocation(
  env: Env,
  jobId: string,
  errorMessage: string,
): Promise<void> {
  const sql = neon(env.NEON_DATABASE_URL);
  await sql`
    UPDATE admin_jobs
    SET
      status = 'failed',
      error = ${errorMessage.slice(0, 1000)},
      finished_at = NOW(),
      updated_at = NOW()
    WHERE id = ${jobId}::uuid
      AND shop_domain = ${env.SHOP_DOMAIN}
  `;
}

export async function recordContentEdit(
  env: Env,
  input: {
    id: string;
    actorEmail: string;
    entityType: "product" | "theme";
    entityId: string;
    resourceId: string;
    fieldKey: string;
    locale: string;
    sourceDigest: string;
    valueSha256: string;
  },
): Promise<void> {
  const sql = neon(env.NEON_DATABASE_URL);
  await sql`
    DELETE FROM admin_content_edits
    WHERE shop_domain = ${env.SHOP_DOMAIN}
      AND created_at < NOW() - INTERVAL '90 days'
  `;
  await sql`
    INSERT INTO admin_content_edits (
      id,
      shop_domain,
      actor_email,
      entity_type,
      entity_id,
      resource_id,
      field_key,
      locale,
      source_digest,
      value_sha256
    )
    VALUES (
      ${input.id}::uuid,
      ${env.SHOP_DOMAIN},
      ${input.actorEmail},
      ${input.entityType},
      ${input.entityId},
      ${input.resourceId},
      ${input.fieldKey},
      ${input.locale},
      ${input.sourceDigest},
      ${input.valueSha256}
    )
  `;
}
