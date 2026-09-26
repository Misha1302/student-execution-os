# Cloudflare Groq relay

A Cloudflare Worker that lets the Student Execution OS API reach Groq from a network
Groq accepts, while users' keys stay server-side (never returned to browsers/phones).

It is **not** a general proxy. It accepts only

    POST /openai/v1/chat/completions   (no query string)

with a valid `X-SEOS-Relay-Token`, a `Authorization: Bearer …` header,
`Content-Type: application/json` and a body of at most 1 MiB, and forwards it to the
hard-coded `https://api.groq.com/openai/v1/chat/completions` with `redirect: "manual"`.
No query parameter or header can choose another scheme, host, port or path.

| Case | Status | `X-SEOS-Relay-Error` |
|---|---|---|
| other path or any query string | 404 | `not_found` |
| not POST | 405 | `method_not_allowed` |
| Worker secret missing/weak | 500 | `relay_misconfigured` |
| wrong/missing relay token | 401 | `unauthorized` |
| no Bearer credential | 400 | `invalid_request` |
| not JSON | 415 | `invalid_request` |
| body > 1 MiB | 413 | `invalid_request` |
| Groq unreachable | 502 | `provider_unreachable` |
| Groq answered a redirect | 502 | `upstream_redirect` |
| **any answer from Groq** | Groq's status | *absent* |

Only `Content-Type`, `Retry-After` and `x-request-id` are passed back from Groq; every
response has `Cache-Control: no-store`.

## Trust

The Worker terminates TLS: Cloudflare can technically see the provider key and prompts
in transit. The code never logs, stores or echoes them, `observability` is disabled in
`wrangler.jsonc`, and the key is sent only to the constant upstream. Avoid `wrangler
tail` on this Worker in production.

## Deploy

```bash
cd deploy/cloudflare-groq-relay
npx wrangler login                 # browser OAuth; never paste API tokens into chat
npx wrangler deploy
# generate the shared secret into a private file and store it without echoing it
umask 077; openssl rand -hex 32 > /secure/place/relay-token
npx wrangler secret put RELAY_TOKEN < /secure/place/relay-token
```

The same value goes into the API secret file `llm-egress-relay.token` (see
`deploy/README.md`, "Cloudflare Groq relay"). Rotate by putting a new secret into both
places, then recreating the API container.

Local tests (no network, no dependencies): `make worker`.
Never commit `.dev.vars` or `.wrangler/`.
