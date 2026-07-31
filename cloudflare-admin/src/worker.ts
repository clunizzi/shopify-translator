import { Hono } from "hono";
import { secureHeaders } from "hono/secure-headers";
import {
  buildThemeJobPayload,
  invokeContentRequest,
  invokeThemeJob,
  type ContentAction,
} from "./aws";
import { requireAccess } from "./auth";
import {
  ActiveJobError,
  createAdminJob,
  failAdminJobInvocation,
  loadOverview,
  recordContentEdit,
} from "./db";
import type {
  AdminJobAction,
  AppBindings,
  DashboardOverview,
} from "./types";
import appScript from "../public/app.client.txt";
import indexHtml from "../public/index.html";
import styles from "../public/styles.css";

const app = new Hono<AppBindings>();
const OVERVIEW_TTL_MS = 30_000;
const JOB_ACTIONS = new Set<AdminJobAction>([
  "theme_audit",
  "theme_canary",
  "theme_sync",
]);
const CONTENT_ACTIONS = new Set<ContentAction>([
  "content_product_search",
  "content_product_inspect",
  "content_product_save",
  "content_theme_resources",
  "content_theme_inspect",
  "content_theme_save",
]);
const CONTENT_WRITE_ACTIONS = new Set<ContentAction>([
  "content_product_save",
  "content_theme_save",
]);

let overviewCache:
  | {
      expiresAt: number;
      value: DashboardOverview;
    }
  | undefined;

app.use("*", secureHeaders());
app.use("*", async (c, next) => {
  const correlationId = c.req.header("Cf-Ray") || crypto.randomUUID();
  c.set("correlationId", correlationId);
  await next();
  c.header("X-Correlation-Id", correlationId);
});

app.use("*", async (c, next) => {
  if (c.env.DEPLOYMENT_LOCKED === "true") {
    return c.json(
      {
        status: "provisioning",
        message: "Cloudflare Access is not configured yet.",
        correlation_id: c.get("correlationId"),
      },
      503,
    );
  }
  await next();
});

app.use("*", requireAccess);

app.get("/", (c) => c.html(indexHtml));
app.get("/index.html", (c) => c.html(indexHtml));
app.get(
  "/styles.css",
  (c) =>
    new Response(styles, {
      headers: {
        "Cache-Control": "no-cache, max-age=0, must-revalidate",
        "Content-Type": "text/css; charset=utf-8",
      },
    }),
);
app.get(
  "/app.js",
  (c) =>
    new Response(appScript, {
      headers: {
        "Cache-Control": "no-cache, max-age=0, must-revalidate",
        "Content-Type": "text/javascript; charset=utf-8",
      },
    }),
);

app.get("/api/health", async (c) => {
  try {
    const overview = await loadOverview(c.env);
    return c.json({
      ok: true,
      database: "reachable",
      shop_domain: c.env.SHOP_DOMAIN,
      approved_theme_id: c.env.APPROVED_THEME_ID,
      scheduled_sync_enabled: c.env.SCHEDULED_SYNC_ENABLED === "true",
      theme_realtime_sync_enabled:
        c.env.THEME_REALTIME_SYNC_ENABLED === "true",
      database_bytes: overview.database_bytes,
      actor: c.get("actorEmail"),
      correlation_id: c.get("correlationId"),
    });
  } catch {
    return c.json(
      {
        ok: false,
        database: "unreachable",
        correlation_id: c.get("correlationId"),
      },
      503,
    );
  }
});

app.get("/api/overview", async (c) => {
  try {
    const now = Date.now();
    if (!overviewCache || overviewCache.expiresAt <= now) {
      overviewCache = {
        value: await loadOverview(c.env),
        expiresAt: now + OVERVIEW_TTL_MS,
      };
    }

    return c.json({
      data: overviewCache.value,
      config: {
        shop_domain: c.env.SHOP_DOMAIN,
        approved_theme_id: c.env.APPROVED_THEME_ID,
        target_locales: c.env.TARGET_LOCALES.split(",")
          .map((locale) => locale.trim())
          .filter(Boolean),
        catalog_poll_schedule: c.env.CATALOG_POLL_SCHEDULE,
        aws_credential_created_at: c.env.AWS_CREDENTIAL_CREATED_AT,
        operations_enabled: Boolean(
          c.env.AWS_ACCESS_KEY_ID &&
          c.env.AWS_SECRET_ACCESS_KEY &&
          c.env.AWS_REGION &&
          c.env.AWS_THEME_FUNCTION_NAME,
        ),
        scheduled_sync_enabled: c.env.SCHEDULED_SYNC_ENABLED === "true",
        theme_realtime_sync_enabled:
          c.env.THEME_REALTIME_SYNC_ENABLED === "true",
        mode: "operational",
      },
      actor: c.get("actorEmail"),
      generated_at: new Date().toISOString(),
      correlation_id: c.get("correlationId"),
    });
  } catch {
    return c.json(
      {
        error: {
          code: "OVERVIEW_UNAVAILABLE",
          message: "The translation overview is temporarily unavailable.",
        },
        correlation_id: c.get("correlationId"),
      },
      503,
    );
  }
});

app.post("/api/content", async (c) => {
  if (
    !c.env.AWS_ACCESS_KEY_ID ||
    !c.env.AWS_SECRET_ACCESS_KEY ||
    !c.env.AWS_REGION ||
    !c.env.AWS_THEME_FUNCTION_NAME
  ) {
    return c.json(
      {
        error: {
          code: "OPERATIONS_DISABLED",
          message: "Il collegamento sicuro a Shopify non è disponibile.",
        },
        correlation_id: c.get("correlationId"),
      },
      503,
    );
  }

  const requestOrigin = c.req.header("Origin");
  const expectedOrigin = new URL(c.req.url).origin;
  const fetchSite = c.req.header("Sec-Fetch-Site");
  if (
    (requestOrigin && requestOrigin !== expectedOrigin) ||
    fetchSite === "cross-site"
  ) {
    return c.json(
      {
        error: {
          code: "CROSS_ORIGIN_REQUEST",
          message: "Le operazioni cross-origin non sono consentite.",
        },
        correlation_id: c.get("correlationId"),
      },
      403,
    );
  }

  const contentLength = Number(c.req.header("Content-Length") || 0);
  if (contentLength > 150_000) {
    return c.json(
      {
        error: {
          code: "REQUEST_TOO_LARGE",
          message: "Il contenuto supera il limite di sicurezza.",
        },
        correlation_id: c.get("correlationId"),
      },
      413,
    );
  }

  let body: Record<string, unknown>;
  try {
    body = await c.req.json();
  } catch {
    return c.json(
      {
        error: {
          code: "INVALID_JSON",
          message: "Serve una richiesta JSON valida.",
        },
        correlation_id: c.get("correlationId"),
      },
      400,
    );
  }
  const action = String(body.action || "") as ContentAction;
  if (!CONTENT_ACTIONS.has(action)) {
    return c.json(
      {
        error: {
          code: "INVALID_ACTION",
          message: "Operazione contenuti non riconosciuta.",
        },
        correlation_id: c.get("correlationId"),
      },
      400,
    );
  }

  try {
    const data = await invokeContentRequest(
      c.env,
      {
        ...body,
        action,
      },
      c.get("actorEmail"),
    );
    let auditRecorded: boolean | null = null;
    if (CONTENT_WRITE_ACTIONS.has(action)) {
      const value = String(body.value || "");
      const valueHash = Array.from(
        new Uint8Array(
          await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value)),
        ),
      )
        .map((byte) => byte.toString(16).padStart(2, "0"))
        .join("");
      try {
        await recordContentEdit(c.env, {
          id: crypto.randomUUID(),
          actorEmail: c.get("actorEmail"),
          entityType: action === "content_product_save" ? "product" : "theme",
          entityId:
            action === "content_product_save"
              ? String(body.product_id || "")
              : c.env.APPROVED_THEME_ID,
          resourceId: String(body.resource_id || ""),
          fieldKey: String(body.key || ""),
          locale: String(body.locale || ""),
          sourceDigest: String(body.digest || ""),
          valueSha256: valueHash,
        });
        auditRecorded = true;
      } catch (auditError) {
        auditRecorded = false;
        console.error("admin_content_audit_failed", {
          correlationId: c.get("correlationId"),
          action,
          message:
            auditError instanceof Error ? auditError.message : "Unknown audit error",
        });
      }
    }
    return c.json(
      {
        data,
        audit_recorded: auditRecorded,
        correlation_id: c.get("correlationId"),
      },
      200,
      {
        "Cache-Control": "no-store",
      },
    );
  } catch (error) {
    const code = error instanceof Error ? error.name : "CONTENT_OPERATION_FAILED";
    const status =
      code === "SOURCE_CHANGED"
        ? 409
        : code === "Error" || code === "CONTENT_OPERATION_FAILED"
          ? 503
          : 400;
    return c.json(
      {
        error: {
          code,
          message:
            error instanceof Error
              ? error.message
              : "Operazione Shopify non completata.",
        },
        correlation_id: c.get("correlationId"),
      },
      status,
      {
        "Cache-Control": "no-store",
      },
    );
  }
});

app.post("/api/jobs/theme", async (c) => {
  if (
    !c.env.AWS_ACCESS_KEY_ID ||
    !c.env.AWS_SECRET_ACCESS_KEY ||
    !c.env.AWS_REGION ||
    !c.env.AWS_THEME_FUNCTION_NAME
  ) {
    return c.json(
      {
        error: {
          code: "OPERATIONS_DISABLED",
          message: "AWS operations are not enabled for this console.",
        },
        correlation_id: c.get("correlationId"),
      },
      503,
    );
  }

  const requestOrigin = c.req.header("Origin");
  const expectedOrigin = new URL(c.req.url).origin;
  const fetchSite = c.req.header("Sec-Fetch-Site");
  if (
    (requestOrigin && requestOrigin !== expectedOrigin) ||
    fetchSite === "cross-site"
  ) {
    return c.json(
      {
        error: {
          code: "CROSS_ORIGIN_REQUEST",
          message: "Cross-origin operations are not allowed.",
        },
        correlation_id: c.get("correlationId"),
      },
      403,
    );
  }

  const contentLength = Number(c.req.header("Content-Length") || 0);
  if (contentLength > 4096) {
    return c.json(
      {
        error: {
          code: "REQUEST_TOO_LARGE",
          message: "The operation request is too large.",
        },
        correlation_id: c.get("correlationId"),
      },
      413,
    );
  }

  let body: { action?: unknown; confirmation?: unknown };
  try {
    body = await c.req.json();
  } catch {
    return c.json(
      {
        error: {
          code: "INVALID_JSON",
          message: "A valid JSON request is required.",
        },
        correlation_id: c.get("correlationId"),
      },
      400,
    );
  }

  const action = String(body.action || "") as AdminJobAction;
  if (!JOB_ACTIONS.has(action)) {
    return c.json(
      {
        error: {
          code: "INVALID_ACTION",
          message: "Unknown theme operation.",
        },
        correlation_id: c.get("correlationId"),
      },
      400,
    );
  }

  const requiredConfirmation =
    action === "theme_sync"
      ? `SYNC ${c.env.APPROVED_THEME_ID}`
      : action === "theme_canary"
        ? `CANARY ${c.env.APPROVED_THEME_ID}`
        : "";
  if (
    requiredConfirmation &&
    String(body.confirmation || "").trim() !== requiredConfirmation
  ) {
    return c.json(
      {
        error: {
          code: "CONFIRMATION_MISMATCH",
          message: `Type ${requiredConfirmation} to confirm this operation.`,
        },
        correlation_id: c.get("correlationId"),
      },
      400,
    );
  }

  const jobId = crypto.randomUUID();
  const actor = c.get("actorEmail");
  const payload = buildThemeJobPayload(c.env, {
    action,
    jobId,
    actor,
  });

  try {
    const job = await createAdminJob(c.env, {
      id: jobId,
      action,
      actorEmail: actor,
      request: {
        theme_id: c.env.APPROVED_THEME_ID,
        target_locales: payload.target_locales,
        max_translations: payload.max_translations ?? null,
        read_only: action === "theme_audit",
      },
    });

    try {
      await invokeThemeJob(c.env, payload);
    } catch (error) {
      const message =
        error instanceof Error ? error.message : "AWS Lambda invocation failed";
      await failAdminJobInvocation(c.env, jobId, message);
      throw error;
    }

    overviewCache = undefined;
    return c.json(
      {
        data: job,
        message: "Operation queued.",
        correlation_id: c.get("correlationId"),
      },
      202,
    );
  } catch (error) {
    if (error instanceof ActiveJobError) {
      return c.json(
        {
          error: {
            code: "JOB_ALREADY_ACTIVE",
            message: "Another theme operation is already queued or running.",
          },
          correlation_id: c.get("correlationId"),
        },
        409,
      );
    }
    return c.json(
      {
        error: {
          code: "JOB_QUEUE_FAILED",
          message: "The operation could not be queued.",
        },
        correlation_id: c.get("correlationId"),
      },
      503,
    );
  }
});

app.notFound(async (c) => {
  if (c.req.path.startsWith("/api/")) {
    return c.json(
      {
        error: {
          code: "NOT_FOUND",
          message: "API route not found.",
        },
        correlation_id: c.get("correlationId"),
      },
      404,
    );
  }
  return c.text("Not found", 404);
});

export default app;
