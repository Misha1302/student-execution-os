// Student Execution OS -> Groq relay (Cloudflare Worker).
//
// This is NOT a general HTTP proxy. It relays exactly one endpoint,
//   POST /openai/v1/chat/completions
// to exactly one fixed provider route through Cloudflare AI Gateway. Nothing in the
// request (query, headers, body)
// can select another scheme, host, port or path.
//
// Trust: the Worker terminates the SEOS -> Worker TLS connection, so it necessarily
// sees the user's Groq key (Authorization) and the request body before opening a new
// TLS connection to Groq. It never logs, stores or echoes either, and forwards the key
// only to the Groq provider route. Worker-generated errors carry X-SEOS-Relay-Error; responses that
// come from Groq never do, so SEOS can tell "relay refused" from "Groq refused".

export const RELAY_PATH = "/openai/v1/chat/completions";
export const AI_GATEWAY_ID = "default";
export const AI_GATEWAY_PROVIDER = "groq";
export const UPSTREAM_PATH = "/chat/completions";
export const MAX_BODY_BYTES = 1 * 1024 * 1024;
export const RELAY_ERROR_HEADER = "X-SEOS-Relay-Error";
// Only these upstream response headers are passed back; everything else is dropped.
const FORWARDED_RESPONSE_HEADERS = ["content-type", "retry-after", "x-request-id"];
const MIN_TOKEN_LENGTH = 32;

function relayError(status, code, extra = {}) {
  return new Response(JSON.stringify({ error: { type: "seos_relay_error", code } }), {
    status,
    headers: {
      "Content-Type": "application/json",
      "Cache-Control": "no-store",
      "X-Content-Type-Options": "nosniff",
      [RELAY_ERROR_HEADER]: code,
      ...extra,
    },
  });
}

async function digest(value) {
  return new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value)));
}

// Constant-time comparison of equal-length SHA-256 digests.
async function tokenMatches(presented, expected) {
  const [a, b] = await Promise.all([digest(presented), digest(expected)]);
  let diff = 0;
  for (let i = 0; i < a.length; i += 1) diff |= a[i] ^ b[i];
  return diff === 0;
}

async function resolveUpstream(env) {
  if (!env?.AI || typeof env.AI.gateway !== "function") return null;
  try {
    const base = await env.AI.gateway(AI_GATEWAY_ID).getUrl(AI_GATEWAY_PROVIDER);
    const url = new URL(base);
    // The binding supplies the account segment. Everything else remains pinned here.
    if (url.protocol !== "https:" || url.hostname !== "gateway.ai.cloudflare.com"
        || url.username !== "" || url.password !== "" || url.port !== ""
        || url.search !== "" || url.hash !== ""
        || !/^\/v1\/[^/]+\/default\/groq\/?$/.test(url.pathname)) return null;
    return `${url.origin}${url.pathname.replace(/\/$/, "")}${UPSTREAM_PATH}`;
  } catch {
    return null;
  }
}

// Reads the body without ever buffering more than MAX_BODY_BYTES (+ one chunk).
async function readLimited(request) {
  if (!request.body) return new Uint8Array(0);
  const reader = request.body.getReader();
  const chunks = [];
  let size = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > MAX_BODY_BYTES) {
      await reader.cancel();
      return null;
    }
    chunks.push(value);
  }
  const body = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    body.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return body;
}

export async function handle(request, env) {
  const url = new URL(request.url);
  // Exact path and no query string: there is no target-selection parameter to parse.
  if (url.pathname !== RELAY_PATH || url.search !== "") return relayError(404, "not_found");
  if (request.method !== "POST") return relayError(405, "method_not_allowed", { Allow: "POST" });

  const expected = typeof env?.RELAY_TOKEN === "string" ? env.RELAY_TOKEN : "";
  if (expected.length < MIN_TOKEN_LENGTH) return relayError(500, "relay_misconfigured");
  const gatewayToken = typeof env?.AI_GATEWAY_TOKEN === "string" ? env.AI_GATEWAY_TOKEN : "";
  if (gatewayToken.length < MIN_TOKEN_LENGTH) return relayError(500, "relay_misconfigured");
  const presented = request.headers.get("X-SEOS-Relay-Token") || "";
  if (!(await tokenMatches(presented, expected))) return relayError(401, "unauthorized");

  const authorization = request.headers.get("Authorization") || "";
  if (!/^Bearer [\x21-\x7e]+$/.test(authorization)) return relayError(400, "invalid_request");

  const contentType = (request.headers.get("Content-Type") || "").split(";", 1)[0].trim().toLowerCase();
  if (contentType !== "application/json") return relayError(415, "invalid_request");

  const declared = request.headers.get("Content-Length");
  if (declared !== null && !(Number(declared) <= MAX_BODY_BYTES)) return relayError(413, "invalid_request");
  const body = await readLimited(request);
  if (body === null) return relayError(413, "invalid_request");

  const upstreamUrl = await resolveUpstream(env);
  if (upstreamUrl === null) return relayError(500, "relay_misconfigured");

  let upstream;
  try {
    upstream = await fetch(upstreamUrl, {
      method: "POST",
      // A fresh header set: nothing from the client except the provider credential.
      // AI Gateway logging and caching are disabled per request because prompts and
      // responses can contain private student data.
      headers: {
        Authorization: authorization,
        "Content-Type": "application/json",
        Accept: "application/json",
        "cf-aig-authorization": `Bearer ${gatewayToken}`,
        "cf-aig-collect-log": "false",
        "cf-aig-skip-cache": "true",
      },
      body,
      // Never follow a redirect: it would carry the provider credential elsewhere.
      redirect: "manual",
    });
  } catch {
    return relayError(502, "provider_unreachable");
  }
  if (upstream.status >= 300 && upstream.status < 400) {
    return relayError(502, "upstream_redirect");
  }

  const headers = new Headers({ "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff" });
  for (const name of FORWARDED_RESPONSE_HEADERS) {
    const value = upstream.headers.get(name);
    if (value !== null) headers.set(name, value);
  }
  return new Response(upstream.body, { status: upstream.status, headers });
}

export default {
  fetch(request, env) {
    return handle(request, env);
  },
};
