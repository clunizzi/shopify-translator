import { describe, expect, it } from "vitest";
import { buildThemeJobPayload } from "../src/aws";
import type { Env } from "../src/types";

const env: Env = {
  NEON_DATABASE_URL: "postgresql://example",
  CF_ACCESS_TEAM_DOMAIN: "https://example.cloudflareaccess.com",
  CF_ACCESS_AUD: "audience",
  AWS_ACCESS_KEY_ID: "access",
  AWS_SECRET_ACCESS_KEY: "secret",
  AWS_REGION: "eu-central-1",
  AWS_THEME_FUNCTION_NAME: "theme-poller",
  AWS_CREDENTIAL_CREATED_AT: "2026-07-30",
  ALLOW_LOCAL_DEV: "true",
  SHOP_DOMAIN: "example-store.myshopify.com",
  SOURCE_LOCALE: "it",
  APPROVED_THEME_ID: "123456789012",
  TARGET_LOCALES: "de, fr",
  CATALOG_POLL_SCHEDULE: "Ogni ora",
  SCHEDULED_SYNC_ENABLED: "false",
  THEME_REALTIME_SYNC_ENABLED: "true",
  DEPLOYMENT_LOCKED: "false",
};

describe("theme operation payloads", () => {
  it("builds a read-only audit bound to the approved theme", () => {
    expect(
      buildThemeJobPayload(env, {
        action: "theme_audit",
        jobId: "job-1",
        actor: "operator@example.com",
      }),
    ).toEqual({
      manual: true,
      action: "theme_audit",
      job_id: "job-1",
      actor: "operator@example.com",
      theme_id: "123456789012",
      target_locales: ["de", "fr"],
      run_theme: true,
      run_global_resources: false,
      dry_run: true,
    });
  });

  it("caps a canary to one translation", () => {
    expect(
      buildThemeJobPayload(env, {
        action: "theme_canary",
        jobId: "job-2",
        actor: "operator@example.com",
      }).max_translations,
    ).toBe(1);
  });

  it("does not cap a full sync", () => {
    const payload = buildThemeJobPayload(env, {
      action: "theme_sync",
      jobId: "job-3",
      actor: "operator@example.com",
    });
    expect(payload.dry_run).toBe(false);
    expect(payload).not.toHaveProperty("max_translations");
  });
});
