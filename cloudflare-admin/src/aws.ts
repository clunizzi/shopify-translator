import { AwsClient } from "aws4fetch";
import type { AdminJobAction, Env } from "./types";

export interface ThemeJobPayload {
  manual: true;
  action: AdminJobAction;
  job_id: string;
  actor: string;
  theme_id: string;
  target_locales: string[];
  run_theme: true;
  run_global_resources: false;
  dry_run: boolean;
  max_translations?: number;
}

export type ContentAction =
  | "content_product_search"
  | "content_product_inspect"
  | "content_product_save"
  | "content_theme_resources"
  | "content_theme_inspect"
  | "content_theme_save";

export interface ContentRequestPayload extends Record<string, unknown> {
  manual: true;
  action: ContentAction;
  actor: string;
  theme_id: string;
}

function required(value: string | undefined, name: string): string {
  const normalized = String(value || "").trim();
  if (!normalized) {
    throw new Error(`${name} is not configured`);
  }
  return normalized;
}

export function buildThemeJobPayload(
  env: Env,
  input: {
    action: AdminJobAction;
    jobId: string;
    actor: string;
  },
): ThemeJobPayload {
  const targetLocales = required(env.TARGET_LOCALES, "TARGET_LOCALES")
    .split(",")
    .map((locale) => locale.trim())
    .filter(Boolean);

  if (!targetLocales.length) {
    throw new Error("TARGET_LOCALES is empty");
  }

  const payload: ThemeJobPayload = {
    manual: true,
    action: input.action,
    job_id: input.jobId,
    actor: input.actor,
    theme_id: required(env.APPROVED_THEME_ID, "APPROVED_THEME_ID"),
    target_locales: targetLocales,
    run_theme: true,
    run_global_resources: false,
    dry_run: input.action === "theme_audit",
  };

  if (input.action === "theme_canary") {
    payload.max_translations = 1;
  }
  return payload;
}

export async function invokeThemeJob(env: Env, payload: ThemeJobPayload): Promise<void> {
  const region = required(env.AWS_REGION, "AWS_REGION");
  const functionName = required(env.AWS_THEME_FUNCTION_NAME, "AWS_THEME_FUNCTION_NAME");
  const client = new AwsClient({
    accessKeyId: required(env.AWS_ACCESS_KEY_ID, "AWS_ACCESS_KEY_ID"),
    secretAccessKey: required(env.AWS_SECRET_ACCESS_KEY, "AWS_SECRET_ACCESS_KEY"),
    service: "lambda",
    region,
    retries: 2,
  });
  const endpoint =
    `https://lambda.${region}.amazonaws.com/2015-03-31/functions/` +
    `${encodeURIComponent(functionName)}/invocations`;
  const response = await client.fetch(endpoint, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Amz-Invocation-Type": "Event",
    },
    body: JSON.stringify(payload),
  });

  if (response.status !== 202) {
    const responseBody = (await response.text()).slice(0, 500);
    throw new Error(`AWS Lambda invocation failed (${response.status}): ${responseBody}`);
  }
}

export async function invokeContentRequest(
  env: Env,
  input: Omit<ContentRequestPayload, "manual" | "actor" | "theme_id"> & {
    action: ContentAction;
  },
  actor: string,
): Promise<unknown> {
  const region = required(env.AWS_REGION, "AWS_REGION");
  const functionName = required(env.AWS_THEME_FUNCTION_NAME, "AWS_THEME_FUNCTION_NAME");
  const client = new AwsClient({
    accessKeyId: required(env.AWS_ACCESS_KEY_ID, "AWS_ACCESS_KEY_ID"),
    secretAccessKey: required(env.AWS_SECRET_ACCESS_KEY, "AWS_SECRET_ACCESS_KEY"),
    service: "lambda",
    region,
    retries: 2,
  });
  const endpoint =
    `https://lambda.${region}.amazonaws.com/2015-03-31/functions/` +
    `${encodeURIComponent(functionName)}/invocations`;
  const payload: ContentRequestPayload = {
    ...input,
    manual: true,
    actor,
    theme_id: required(env.APPROVED_THEME_ID, "APPROVED_THEME_ID"),
  };
  const response = await client.fetch(endpoint, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Amz-Invocation-Type": "RequestResponse",
    },
    body: JSON.stringify(payload),
  });
  const raw = await response.text();
  if (!response.ok || response.headers.get("X-Amz-Function-Error")) {
    throw new Error(`AWS Lambda request failed (${response.status})`);
  }
  let decoded: unknown;
  try {
    decoded = JSON.parse(raw);
  } catch {
    throw new Error("AWS Lambda returned an invalid response");
  }
  if (
    typeof decoded === "object" &&
    decoded !== null &&
    "ok" in decoded &&
    decoded.ok === false
  ) {
    const error =
      "error" in decoded && typeof decoded.error === "object" && decoded.error !== null
        ? decoded.error
        : {};
    const code =
      "code" in error && typeof error.code === "string"
        ? error.code
        : "CONTENT_OPERATION_FAILED";
    const message =
      "message" in error && typeof error.message === "string"
        ? error.message
        : "Shopify ha rifiutato l’operazione.";
    const operationError = new Error(message);
    operationError.name = code;
    throw operationError;
  }
  if (
    typeof decoded !== "object" ||
    decoded === null ||
    !("ok" in decoded) ||
    decoded.ok !== true ||
    !("data" in decoded)
  ) {
    throw new Error("AWS Lambda returned an unexpected response");
  }
  return decoded.data;
}
