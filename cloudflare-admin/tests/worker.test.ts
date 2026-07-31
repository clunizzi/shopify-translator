import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Env } from "../src/types";

const {
  createAdminJob,
  failAdminJobInvocation,
  invokeThemeJob,
  invokeContentRequest,
  recordContentEdit,
} = vi.hoisted(
  () => ({
    createAdminJob: vi.fn(),
    failAdminJobInvocation: vi.fn(),
    invokeThemeJob: vi.fn(),
    invokeContentRequest: vi.fn(),
    recordContentEdit: vi.fn(),
  }),
);

vi.mock("../src/db", () => ({
  ActiveJobError: class ActiveJobError extends Error {},
  createAdminJob,
  failAdminJobInvocation,
  loadOverview: vi.fn(),
  recordContentEdit,
}));

vi.mock("../src/aws", async (importOriginal) => {
  const original = await importOriginal<typeof import("../src/aws")>();
  return {
    ...original,
    invokeThemeJob,
    invokeContentRequest,
  };
});

const { default: app } = await import("../src/worker");

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
  TARGET_LOCALES: "de,fr",
  CATALOG_POLL_SCHEDULE: "Ogni ora",
  SCHEDULED_SYNC_ENABLED: "false",
  THEME_REALTIME_SYNC_ENABLED: "true",
  DEPLOYMENT_LOCKED: "false",
};

describe("theme operation API", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    createAdminJob.mockResolvedValue({
      id: "11111111-1111-4111-8111-111111111111",
      action: "theme_sync",
      status: "queued",
    });
    invokeThemeJob.mockResolvedValue(undefined);
    invokeContentRequest.mockResolvedValue({ products: [] });
    recordContentEdit.mockResolvedValue(undefined);
  });

  it("rejects a live sync without the exact theme confirmation", async () => {
    const response = await app.request(
      "http://localhost/api/jobs/theme",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          action: "theme_sync",
          confirmation: "SYNC 999",
        }),
      },
      env,
    );

    expect(response.status).toBe(400);
    expect(createAdminJob).not.toHaveBeenCalled();
    expect(invokeThemeJob).not.toHaveBeenCalled();
  });

  it("forces frontend assets to revalidate after a deploy", async () => {
    const response = await app.request("http://localhost/app.js", {}, env);

    expect(response.status).toBe(200);
    expect(response.headers.get("Cache-Control")).toBe(
      "no-cache, max-age=0, must-revalidate",
    );
  });

  it("queues an audit without requiring a write confirmation", async () => {
    const response = await app.request(
      "http://localhost/api/jobs/theme",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "theme_audit" }),
      },
      env,
    );

    expect(response.status).toBe(202);
    expect(createAdminJob).toHaveBeenCalledOnce();
    expect(invokeThemeJob).toHaveBeenCalledWith(
      env,
      expect.objectContaining({
        action: "theme_audit",
        dry_run: true,
        theme_id: "123456789012",
      }),
    );
  });

  it("rejects browser requests from another origin", async () => {
    const response = await app.request(
      "http://localhost/api/jobs/theme",
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Origin: "https://evil.example",
        },
        body: JSON.stringify({ action: "theme_audit" }),
      },
      env,
    );

    expect(response.status).toBe(403);
    expect(invokeThemeJob).not.toHaveBeenCalled();
  });

  it("serves read-only product inspection synchronously", async () => {
    invokeContentRequest.mockResolvedValue({
      product: { id: "gid://shopify/Product/123", title: "Motosega" },
      fields: [],
    });
    const response = await app.request(
      "http://localhost/api/content",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          action: "content_product_inspect",
          product_id: "gid://shopify/Product/123",
        }),
      },
      env,
    );

    expect(response.status).toBe(200);
    expect(invokeContentRequest).toHaveBeenCalledWith(
      env,
      expect.objectContaining({
        action: "content_product_inspect",
        product_id: "gid://shopify/Product/123",
      }),
      "local-development",
    );
    expect(recordContentEdit).not.toHaveBeenCalled();
  });

  it("records only hashes and identifiers after a point edit", async () => {
    invokeContentRequest.mockResolvedValue({
      saved: {
        resource_id: "gid://shopify/Product/123",
        key: "title",
        locale: "de",
        status: "current",
      },
    });
    const response = await app.request(
      "http://localhost/api/content",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          action: "content_product_save",
          product_id: "gid://shopify/Product/123",
          resource_id: "gid://shopify/Product/123",
          key: "title",
          locale: "de",
          value: "Kettensäge",
          digest: "digest-1",
        }),
      },
      env,
    );

    expect(response.status).toBe(200);
    expect(recordContentEdit).toHaveBeenCalledWith(
      env,
      expect.objectContaining({
        entityType: "product",
        entityId: "gid://shopify/Product/123",
        fieldKey: "title",
        locale: "de",
        sourceDigest: "digest-1",
        valueSha256: expect.stringMatching(/^[a-f0-9]{64}$/),
      }),
    );
    const auditInput = recordContentEdit.mock.calls[0][1];
    expect(auditInput).not.toHaveProperty("value");
  });

  it("rejects cross-origin content writes before AWS", async () => {
    const response = await app.request(
      "http://localhost/api/content",
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Origin: "https://evil.example",
        },
        body: JSON.stringify({
          action: "content_theme_save",
          value: "Hallo",
        }),
      },
      env,
    );

    expect(response.status).toBe(403);
    expect(invokeContentRequest).not.toHaveBeenCalled();
  });
});
