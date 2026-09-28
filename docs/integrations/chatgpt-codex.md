# Connecting ChatGPT, Codex and other MCP clients

botay! exposes a [Model Context Protocol](https://modelcontextprotocol.io) server at
`https://<your botay! server>/mcp`. An external assistant connected there can read your
data and make changes **only with the permissions you grant**, and every change goes
through exactly the same checks as the app (see ADR 0030/0031).

## What an assistant can do

| Scope | Allows |
|---|---|
| `today:read` | Today: plan, due tasks, classes/events (notes only with `notes:read`) |
| `tasks:read` / `calendar:read` / `notes:read` / `reminders:read` | read that data |
| `tasks:write` | create and change tasks (create, update, start, progress, complete, defer, …) |
| `events:write` | create and change personal events |
| `notes:write` | create and change notes |
| `reminders:write` | create and change reminders |
| `schedule:write` | personal changes to single classes (move, cancel, restore) |
| `destructive` | permanently delete — needed **in addition** to the matching `:write` scope |

Not available to assistants at all: projects, routines, planning constraints,
calibration, focus sessions, creating/splitting class series, holidays, account
settings, AI keys, other connections.

MCP tools: `get_capabilities`, `get_today`, `list_tasks`, `get_calendar`, `list_events`,
`list_notes`, `get_note`, `list_reminders`, `create_task`, `create_note`,
`apply_operations`. `tools/list` only shows tools your grant can use.

## ChatGPT

ChatGPT connects to remote MCP servers from its connector / developer-mode settings
(the exact menu and which plans have it change over time — check OpenAI's current help
page). botay! supports the OAuth sign-in ChatGPT uses for authenticated servers:

1. In ChatGPT, add a custom connector with the URL `https://<server>/mcp` and choose
   **OAuth**. ChatGPT discovers botay!'s OAuth endpoints automatically
   (`/.well-known/oauth-protected-resource`, `/.well-known/oauth-authorization-server`)
   and registers itself.
2. ChatGPT opens botay!. Sign in if asked; you then see **"Allow ChatGPT to use your
   botay!?"** with the permissions it asked for. Untick anything you don't want, pick how
   long the connection lasts (30/90/365 days) and press **Allow**.
3. You are returned to ChatGPT. The connection appears in botay! under
   **Settings → Connected apps**, where **Disconnect** revokes it immediately.

If your ChatGPT workspace instead offers API-key / bearer authentication for a
connector, you can create a token in **Settings → Connected apps → Create an access
token** and paste it there.

## Codex

Codex (CLI/IDE) reads MCP servers from `~/.codex/config.toml` (or a trusted project's
`.codex/config.toml`) and can send a bearer token taken from an environment variable:

```toml
[mcp_servers.botay]
url = "https://<server>/mcp"
bearer_token_env_var = "BOTAY_TOKEN"
```

Create the token in **Settings → Connected apps → Create an access token** (it is shown
once), then `export BOTAY_TOKEN=botay_cap_…` in the environment Codex runs in. Keep the
token out of `config.toml` and out of git. Equivalent command:
`codex mcp add botay --url https://<server>/mcp --bearer-token-env-var BOTAY_TOKEN`.
Codex can also use the OAuth flow above; loopback redirect URIs
(`http://127.0.0.1:<port>/…`) are accepted for native clients.

## How changes behave

- Every change is a typed operation with a client `op_id`. Retrying with the same
  `op_id` never applies it twice (also if the app's offline queue sends the same one).
- Edits change only the fields sent. A lifecycle change that disagrees with the current
  state — e.g. starting a task you cancelled on your phone — comes back as `CONFLICT` and
  is not forced; the assistant should re-read and ask you.
- A batch containing one operation outside the grant is refused as a whole; nothing in
  it is applied.
- Deleting needs the `destructive` scope. Without it, an assistant can archive or cancel
  (reversible) but not delete.
- Changes made by an assistant are recorded with their connection
  (`principal_id = grant:<id>`, actor `USER_VIA_LLM`).

## Revoking

**Settings → Connected apps → Disconnect** revokes a connection on the next request.
Deleting your account deletes every connection. Tokens are stored only as hashes; a
lost token cannot be shown again — create a new one and disconnect the old one.

## Operators

- Behind a reverse proxy keep `SEOS_PROXY_HEADERS=1` (Host and X-Forwarded-Proto are
  forwarded by the shipped nginx/Caddy configs), or set `SEOS_PUBLIC_ORIGIN` to the public
  `https://` origin so OAuth metadata advertises the right URLs.
- `POST /oauth/register` is unauthenticated by design (RFC 7591) and rate-limited per IP
  (10/min, per process).
