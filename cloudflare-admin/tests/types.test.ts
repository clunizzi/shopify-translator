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
    };

    expect(overview.product_translations).toEqual([]);
    expect(overview.theme_sources).toEqual([]);
    expect(overview.recent_translations).toEqual([]);
    expect(overview.recent_theme_events).toEqual([]);
    expect(overview.jobs).toEqual([]);
  });
});
