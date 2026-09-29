// Deterministic tests for the relay Worker: `node --test deploy/cloudflare-groq-relay/test/*.test.js`.
// The global fetch is replaced, so nothing leaves the machine.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { afterEach, beforeEach, test } from "node:test";

import worker, {
  AI_GATEWAY_ID, AI_GATEWAY_PROVIDER, MAX_BODY_BYTES, RELAY_PATH, UPSTREAM_PATH,
} from "../src/index.js";

const TOKEN = "t".repeat(64);
const GATEWAY_TOKEN = "g".repeat(64);
const GATEWAY_BASE = "https://gateway.ai.cloudflare.com/v1/0123456789abcdef0123456789abcdef/default/groq";
const UPSTREAM = GATEWAY_BASE + UPSTREAM_PATH;
const ENV = {
  RELAY_TOKEN: TOKEN,
  AI_GATEWAY_TOKEN: GATEWAY_TOKEN,
  AI: {
    gateway(id) {
      assert.equal(id, AI_GATEWAY_ID);
      return {
        async getUrl(provider) {
          assert.equal(provider, AI_GATEWAY_PROVIDER);
          return GATEWAY_BASE;
        },
      };
    },
  },
};
const BASE = "https://seos-groq-relay.example.workers.dev";
const realFetch = globalThis.fetch;
let calls;

function upstreamAnswers(status, body = "{}", headers = {}) {
  globalThis.fetch = async (url, init) => {
    calls.push({ url, init });
    return new Response(body, { status, headers: { "Content-Type": "application/json", ...headers } });
  };
}

function relayRequest({ path = RELAY_PATH, method = "POST", headers = {}, body = '{"model":"m"}' } = {}) {
  return new Request(BASE + path, {
    method,
    headers: {
      "Content-Type": "application/json",
      Authorization: "Bearer gsk-test",
      "X-SEOS-Relay-Token": TOKEN,
      ...headers,
    },
    body: method === "GET" || method === "HEAD" ? undefined : body,
  });
}

beforeEach(() => {
  calls = [];
  upstreamAnswers(200, '{"choices":[]}');
});
afterEach(() => {
  globalThis.fetch = realFetch;
});

test("root and other paths are 404 with a relay error", async () => {
  for (const path of ["/", "/openai/v1/models", "/openai/v1/chat/completions/", "/OPENAI/v1/chat/completions"]) {
    const response = await worker.fetch(relayRequest({ path }), ENV);
    assert.equal(response.status, 404, path);
    assert.equal(response.headers.get("X-SEOS-Relay-Error"), "not_found");
    assert.equal(response.headers.get("Cache-Control"), "no-store");
  }
  assert.equal(calls.length, 0);
});

test("only POST is accepted", async () => {
  for (const method of ["GET", "PUT", "DELETE", "PATCH"]) {
    const response = await worker.fetch(relayRequest({ method }), ENV);
    assert.equal(response.status, 405, method);
    assert.equal(response.headers.get("X-SEOS-Relay-Error"), "method_not_allowed");
  }
  assert.equal(calls.length, 0);
});

test("the relay token is required and compared exactly", async () => {
  for (const token of [undefined, "", "wrong", TOKEN + "x", TOKEN.slice(1)]) {
    const headers = token === undefined ? { "X-SEOS-Relay-Token": "" } : { "X-SEOS-Relay-Token": token };
    const response = await worker.fetch(relayRequest({ headers }), ENV);
    assert.equal(response.status, 401, String(token));
    assert.equal(response.headers.get("X-SEOS-Relay-Error"), "unauthorized");
    assert.ok(!(await response.text()).includes(TOKEN));
  }
  assert.equal(calls.length, 0);
});

test("a missing or weak Worker secret fails closed", async () => {
  for (const env of [{}, { RELAY_TOKEN: "" }, { RELAY_TOKEN: "short" }, undefined]) {
    const response = await worker.fetch(relayRequest({ headers: { "X-SEOS-Relay-Token": "short" } }), env);
    assert.equal(response.status, 500);
    assert.equal(response.headers.get("X-SEOS-Relay-Error"), "relay_misconfigured");
  }
  assert.equal(calls.length, 0);
});

test("a missing or weak AI Gateway token fails closed", async () => {
  for (const gatewayToken of [undefined, "", "short"]) {
    const env = { ...ENV };
    if (gatewayToken === undefined) delete env.AI_GATEWAY_TOKEN;
    else env.AI_GATEWAY_TOKEN = gatewayToken;
    const response = await worker.fetch(relayRequest(), env);
    assert.equal(response.status, 500);
    assert.equal(response.headers.get("X-SEOS-Relay-Error"), "relay_misconfigured");
  }
  assert.equal(calls.length, 0);
});

test("provider Authorization and JSON content type are required", async () => {
  let response = await worker.fetch(relayRequest({ headers: { Authorization: "" } }), ENV);
  assert.equal(response.status, 400);
  assert.equal(response.headers.get("X-SEOS-Relay-Error"), "invalid_request");
  response = await worker.fetch(relayRequest({ headers: { Authorization: "Basic abc" } }), ENV);
  assert.equal(response.status, 400);
  response = await worker.fetch(relayRequest({ headers: { "Content-Type": "text/plain" } }), ENV);
  assert.equal(response.status, 415);
  assert.equal(response.headers.get("X-SEOS-Relay-Error"), "invalid_request");
  assert.equal(calls.length, 0);
});

test("bodies over the limit are refused before contacting Groq", async () => {
  const response = await worker.fetch(relayRequest({ body: "x".repeat(MAX_BODY_BYTES + 1) }), ENV);
  assert.equal(response.status, 413);
  assert.equal(response.headers.get("X-SEOS-Relay-Error"), "invalid_request");
  assert.equal(calls.length, 0);
  assert.equal(MAX_BODY_BYTES, 1024 * 1024);
});

test("a valid request goes only to the hard-coded upstream with a fresh header set", async () => {
  const response = await worker.fetch(relayRequest({
    headers: { "X-Target-Host": "evil.example", Host: "evil.example", "X-Forwarded-Host": "evil.example",
               Cookie: "a=b" },
  }), ENV);
  assert.equal(response.status, 200);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, UPSTREAM);
  assert.equal(AI_GATEWAY_ID, "default");
  assert.equal(AI_GATEWAY_PROVIDER, "groq");
  assert.equal(UPSTREAM_PATH, "/chat/completions");
  assert.equal(calls[0].init.method, "POST");
  assert.equal(calls[0].init.redirect, "manual");
  assert.deepEqual(Object.keys(calls[0].init.headers).sort(), [
    "Accept", "Authorization", "Content-Type", "cf-aig-authorization", "cf-aig-collect-log",
    "cf-aig-skip-cache",
  ]);
  assert.equal(calls[0].init.headers.Authorization, "Bearer gsk-test");
  assert.equal(calls[0].init.headers["cf-aig-authorization"], `Bearer ${GATEWAY_TOKEN}`);
  assert.equal(calls[0].init.headers["cf-aig-collect-log"], "false");
  assert.equal(calls[0].init.headers["cf-aig-skip-cache"], "true");
  assert.equal(new TextDecoder().decode(calls[0].init.body), '{"model":"m"}');
  assert.equal(response.headers.get("Cache-Control"), "no-store");
  assert.equal(response.headers.get("X-SEOS-Relay-Error"), null);
});

test("query strings cannot select a target", async () => {
  for (const query of ["?url=https://example.com", "?target=https://example.com", "?"]) {
    const response = await worker.fetch(relayRequest({ path: RELAY_PATH + query }), ENV);
    // "?" alone is normalised away by URL; it then reaches the fixed upstream.
    if (query === "?") {
      assert.equal(response.status, 200);
      continue;
    }
    assert.equal(response.status, 404, query);
    assert.equal(response.headers.get("X-SEOS-Relay-Error"), "not_found");
  }
  assert.deepEqual(calls.map((call) => call.url), [UPSTREAM]);
});

test("Groq errors are forwarded without a relay error header", async () => {
  for (const status of [400, 401, 403, 404, 429, 500, 503]) {
    upstreamAnswers(status, '{"error":{"code":"x"}}', { "Retry-After": "7", "x-request-id": "req_1",
                                                         "Set-Cookie": "c=1", Server: "cloudflare", "cf-ray": "abc" });
    const response = await worker.fetch(relayRequest(), ENV);
    assert.equal(response.status, status);
    assert.equal(response.headers.get("X-SEOS-Relay-Error"), null, String(status));
    assert.equal(response.headers.get("Cache-Control"), "no-store");
    assert.equal(response.headers.get("Retry-After"), "7");
    assert.equal(response.headers.get("x-request-id"), "req_1");
    assert.equal(response.headers.get("Set-Cookie"), null);
    assert.equal(response.headers.get("cf-ray"), null);
    assert.equal(await response.text(), '{"error":{"code":"x"}}');
  }
});

test("an upstream redirect is not followed and not passed through", async () => {
  upstreamAnswers(302, "", { Location: "https://evil.example/steal" });
  const response = await worker.fetch(relayRequest(), ENV);
  assert.equal(response.status, 502);
  assert.equal(response.headers.get("X-SEOS-Relay-Error"), "upstream_redirect");
  assert.equal(response.headers.get("Location"), null);
  assert.equal(calls.length, 1);
});

test("an unreachable upstream is a relay error", async () => {
  globalThis.fetch = async () => {
    throw new TypeError("network");
  };
  const response = await worker.fetch(relayRequest(), ENV);
  assert.equal(response.status, 502);
  assert.equal(response.headers.get("X-SEOS-Relay-Error"), "provider_unreachable");
});

test("a missing, broken or unexpected AI Gateway binding fails closed", async () => {
  const badEnvironments = [
    { RELAY_TOKEN: TOKEN, AI_GATEWAY_TOKEN: GATEWAY_TOKEN },
    { RELAY_TOKEN: TOKEN, AI_GATEWAY_TOKEN: GATEWAY_TOKEN, AI: {} },
    { RELAY_TOKEN: TOKEN, AI_GATEWAY_TOKEN: GATEWAY_TOKEN, AI: { gateway: () => ({ getUrl: async () => "https://evil.example/groq" }) } },
    { RELAY_TOKEN: TOKEN, AI_GATEWAY_TOKEN: GATEWAY_TOKEN, AI: { gateway: () => ({ getUrl: async () => `${GATEWAY_BASE}/other` }) } },
    { RELAY_TOKEN: TOKEN, AI_GATEWAY_TOKEN: GATEWAY_TOKEN, AI: { gateway: () => ({ getUrl: async () => { throw new Error("broken"); } }) } },
  ];
  for (const env of badEnvironments) {
    const response = await worker.fetch(relayRequest(), env);
    assert.equal(response.status, 500);
    assert.equal(response.headers.get("X-SEOS-Relay-Error"), "relay_misconfigured");
  }
  assert.equal(calls.length, 0);
});

test("the source never logs and has no generic target parsing", () => {
  const source = readFileSync(new URL("../src/index.js", import.meta.url), "utf8");
  assert.doesNotMatch(source, /console\./);
  // One outbound call site; the other "fetch(" is the handler definition itself.
  assert.equal((source.match(/\bfetch\(/g) || []).length, 2);
  assert.match(source, /^  fetch\(request, env\) \{$/m);
  assert.match(source, /await fetch\(upstreamUrl,/);
  assert.match(source, /redirect: "manual"/);
  assert.match(source, /gateway\(AI_GATEWAY_ID\)\.getUrl\(AI_GATEWAY_PROVIDER\)/);
  assert.match(source, /"cf-aig-collect-log": "false"/);
  assert.match(source, /"cf-aig-skip-cache": "true"/);
  assert.doesNotMatch(source, /searchParams|X-Target|x-target|\.get\("url"\)/);
  const config = readFileSync(new URL("../wrangler.jsonc", import.meta.url), "utf8");
  assert.match(config, /"name": "seos-groq-relay"/);
  assert.match(config, /"observability": \{ "enabled": false \}/);
  assert.match(config, /"ai": \{ "binding": "AI" \}/);
  assert.doesNotMatch(config, /RELAY_TOKEN"\s*:|"vars"/);
});

test("path confusion cannot reach another route or host", async () => {
  for (const path of [
    "/openai/v1/chat%2Fcompletions", "/openai/v1/chat/completions%00", "//openai/v1/chat/completions",
    "/openai/v1/./chat/completions/..", "/openai/v1/chat/completions;x=1", "/@evil.example/openai/v1/chat/completions",
    "/openai/v1/chat/completions/../../../v1/models", "/openai/v1/chat/completions%2F..%2Fmodels",
  ]) {
    const response = await worker.fetch(relayRequest({ path }), ENV);
    const url = new URL(BASE + path);
    if (url.pathname === RELAY_PATH && url.search === "") {
      // URL normalisation resolved it to the one route; it can still only reach UPSTREAM.
      assert.equal(response.status, 200, path);
    } else {
      assert.equal(response.status, 404, path);
      assert.equal(response.headers.get("X-SEOS-Relay-Error"), "not_found");
    }
  }
  assert.ok(calls.every((call) => call.url === UPSTREAM));
});

test("a streamed body without Content-Length is still bounded", async () => {
  const chunk = new Uint8Array(256 * 1024);
  let sent = 0;
  const body = new ReadableStream({
    pull(controller) {
      if (sent > MAX_BODY_BYTES * 2) return controller.close();
      sent += chunk.byteLength;
      controller.enqueue(chunk);
    },
  });
  const request = new Request(BASE + RELAY_PATH, {
    method: "POST", body, duplex: "half",
    headers: { "Content-Type": "application/json", Authorization: "Bearer gsk-test", "X-SEOS-Relay-Token": TOKEN },
  });
  assert.equal(request.headers.get("Content-Length"), null);
  const response = await worker.fetch(request, ENV);
  assert.equal(response.status, 413);
  assert.equal(calls.length, 0);
  assert.ok(sent <= MAX_BODY_BYTES + 2 * chunk.byteLength, "stopped reading soon after the limit");
});

test("only allow-listed upstream response headers are returned", async () => {
  upstreamAnswers(429, '{"error":{"code":"rate_limit_exceeded"}}', {
    "Retry-After": "7", "x-request-id": "req_1", "Set-Cookie": "__cf=secret", Location: "https://evil.example",
    "Access-Control-Allow-Origin": "*", "x-ratelimit-remaining-tokens": "0", Server: "cloudflare",
  });
  const response = await worker.fetch(relayRequest(), ENV);
  assert.equal(response.status, 429);
  assert.equal(response.headers.get("Retry-After"), "7");
  assert.equal(response.headers.get("x-request-id"), "req_1");
  for (const name of ["Set-Cookie", "Location", "Access-Control-Allow-Origin", "x-ratelimit-remaining-tokens", "Server"]) {
    assert.equal(response.headers.get(name), null, name);
  }
  assert.equal(response.headers.get("Cache-Control"), "no-store");
  assert.equal(response.headers.get("X-SEOS-Relay-Error"), null);
});

test("a relay token of the wrong length or case is refused before Groq", async () => {
  for (const token of ["", TOKEN.slice(1), TOKEN + "t", TOKEN.toUpperCase(), "x".repeat(64)]) {
    const response = await worker.fetch(relayRequest({ headers: { "X-SEOS-Relay-Token": token } }), ENV);
    assert.equal(response.status, 401, JSON.stringify(token.length));
    assert.equal(response.headers.get("X-SEOS-Relay-Error"), "unauthorized");
    assert.equal(await response.text(), '{"error":{"type":"seos_relay_error","code":"unauthorized"}}');
  }
  assert.equal(calls.length, 0);
});
