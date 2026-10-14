import { describe, expect, it } from "vitest";
import type { DashboardOverview } from "../src/types";

describe("dashboard contract", () => {
  it("represents a read-only empty store without nullable collections", () => {
    const overview: DashboardOverview = {
      database_bytes: "0",
      product_sources: 0,
      product_source_updated_at: null,
      product_translations: [],
      translation_memory: 0,
      dictionary_entries: 0,
      theme_sources: [],
      theme_translations: [],
      recent_translations: [],
      recent_theme_events: [],
      jobs: [],
      usage: {
        today_usd: 0,
        seven_days_usd: 0,
        month_usd: 0,
        thirty_days_usd: 0,
        openai_calls_30d: 0,
        fallback_calls_30d: 0,
        ai_events_30d: 0,
        no_ai_events_30d: 0,
        product_ai_events_30d: 0,
        avg_product_usd_30d: 0,
        prompt_tokens_30d: 0,
        cached_prompt_tokens_30d: 0,
        cache_write_tokens_30d: 0,
        completion_tokens_30d: 0,
        latest_event_at: null,
        estimates_complete: true,
        pricing_date: null,
        daily: [],
        models: [],
        recent: [],
      },
    };

    expect(overview.product_translations).toEqual([]);
    expect(overview.theme_sources).toEqual([]);
    expect(overview.recent_translations).toEqual([]);
    expect(overview.recent_theme_events).toEqual([]);
    expect(overview.jobs).toEqual([]);
    expect(overview.usage.thirty_days_usd).toBe(0);
  });
});
