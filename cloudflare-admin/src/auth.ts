import { createRemoteJWKSet, jwtVerify, type JWTPayload } from "jose";
import type { Context, Next } from "hono";
import type { AppBindings, Env } from "./types";

const jwksByTeam = new Map<string, ReturnType<typeof createRemoteJWKSet>>();

function normalizeTeamDomain(value: string): string {
  return value.trim().replace(/\/+$/, "");
}

function isLocalRequest(request: Request): boolean {
  const hostname = new URL(request.url).hostname;
  return hostname === "127.0.0.1" || hostname === "localhost";
}

function actorFromPayload(payload: JWTPayload): string {
  if (typeof payload.email === "string" && payload.email) {
    return payload.email;
  }
  return typeof payload.sub === "string" && payload.sub ? payload.sub : "cloudflare-access-user";
}

async function verifyAccessToken(token: string, env: Env): Promise<JWTPayload> {
  const teamDomain = normalizeTeamDomain(env.CF_ACCESS_TEAM_DOMAIN || "");
  const audience = (env.CF_ACCESS_AUD || "").trim();
  if (!teamDomain || !audience) {
    throw new Error("Cloudflare Access is not configured");
  }

  let jwks = jwksByTeam.get(teamDomain);
  if (!jwks) {
    jwks = createRemoteJWKSet(new URL(`${teamDomain}/cdn-cgi/access/certs`));
    jwksByTeam.set(teamDomain, jwks);
  }

  const { payload } = await jwtVerify(token, jwks, {
    audience,
    issuer: teamDomain,
  });
  return payload;
}

export async function requireAccess(c: Context<AppBindings>, next: Next): Promise<Response | void> {
  if (c.env.ALLOW_LOCAL_DEV === "true" && isLocalRequest(c.req.raw)) {
    c.set("actorEmail", "local-development");
    await next();
    return;
  }

  const token = c.req.header("Cf-Access-Jwt-Assertion") || "";
  if (!token) {
    return c.json(
      {
        error: {
          code: "ACCESS_TOKEN_MISSING",
          message: "Cloudflare Access authentication is required.",
        },
        correlation_id: c.get("correlationId"),
      },
      401,
    );
  }

  try {
    const payload = await verifyAccessToken(token, c.env);
    c.set("actorEmail", actorFromPayload(payload));
    await next();
  } catch {
    return c.json(
      {
        error: {
          code: "ACCESS_TOKEN_INVALID",
          message: "Cloudflare Access authentication failed.",
        },
        correlation_id: c.get("correlationId"),
      },
      401,
    );
  }
}
