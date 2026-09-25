# ADR 0017 — Per-account LLM credentials (BYOK) and the platform-managed seam

## Status

Accepted, 2026-09-25. Schema v14.

## Context

Until v13 the Assistant used one server-wide provider configured through
`SEOS_LLM_*`. Every account's inference was billed to the operator's key. The free
product cannot assume the operator pays for all users, and a future paid tier must
be able to include AI without asking the user for a key.

## Decision

### Credential sources

`agent/credentials.py` resolves, per account and per request, one of:

| Source | Who supplies credentials | When |
|---|---|---|
| `USER_BYOK` | the account holder, in Settings → AI | a readable key is stored for the account |
| `PLATFORM_MANAGED` | the operator (`SEOS_PLATFORM_LLM_*`) | the account has an unexpired `llm_entitlements` row **and** platform credentials are configured |
| `NONE` | — | otherwise: the deterministic RU/EN parser |

A user's own key wins over an entitlement (explicit user choice). Platform
credentials alone serve nobody: without an entitlement they are never used, so the
old "one key for everyone" behaviour cannot come back by configuration accident. The
legacy `SEOS_LLM_*` variables are ignored and the server says so at startup.

Every source builds its provider through the same `build_provider`; the Assistant
boundary (typed proposals, validation, confirmation, idempotent apply — ADR 0007) is
unchanged. An LLM stays an enhancement: without one, and on any provider failure,
capture uses the local parser (`fallback_reason` tells the client why).

### Storage

- `llm_credentials` (one row per account, `ON DELETE CASCADE`): provider, model,
  base URL, AES-256-GCM ciphertext + nonce, master-key id, masked hint, status.
- The master key is **not** in the database: `SEOS_CREDENTIAL_KEY_FILE` (a file of
  base64 32-byte keys, first = active) mounted only into the API container. Database
  files, backups and exports therefore never hold a usable key.
- Associated data = `account_id | provider | base_url`: a row copied to another
  account, or re-pointed to another host by SQL, fails authentication and is
  reported as `UNREADABLE` rather than used.
- Without a master key the server degrades: saving is refused
  (`UNSUPPORTED_CAPABILITY`), everything else works. There is no plaintext fallback.
- Rotation: `credential-key-generate --rotate`, restart, `credentials-rekey`, then
  drop the old key line.

### API and lifecycle

| Step | Surface |
|---|---|
| set / update | `PUT /api/v1/settings/llm` `{provider, model, api_key?, base_url?, expected_version?}` |
| read | `GET /api/v1/settings/llm` — masked `key_hint` only, never the key |
| test | `POST /api/v1/settings/llm/test` — one minimal provider request; ≤ 6/min per account |
| use | `/api/v1/assistant/interpret` resolves the account's source; 401/404 results update the stored status |
| revoke | `DELETE /api/v1/settings/llm` |
| account deletion | row purged with the account; export never includes it |

Changing the provider or the API address requires re-entering the key, so a stolen
session cannot redirect a stored key to a host it controls. Only the model can be
changed while keeping the key.

### Leak prevention

- Providers hold the key in a `repr=False` field; provider errors carry a reason
  code and HTTP status, never the provider's response body (some echo key
  fragments); redirects are not followed.
- FastAPI's default 422 body (which echoes the submitted input) is replaced by one
  that names only the failing location.
- Keys never enter the offline sync queue or device storage; the Settings screen
  loads AI settings without the read cache.
- User-supplied base URLs (OpenAI-compatible only) must be public `https` hosts; the
  resolved addresses are checked when saving and before every request (no loopback,
  private, link-local/metadata or other non-global targets).

### Platform-managed seam

`llm_entitlements(account_id, source='PLATFORM_MANAGED', plan, granted_at,
expires_at)` is written only by an operator (`llm-entitlement` CLI) today; a future
billing integration writes the same row. Usage accounting, quotas and spend limits
are a roadmap item (`docs/ROADMAP.md`) and must exist before any real plan grants
platform-managed access.

## Rollback

Migration 014 only adds two tables. Rolling the code back to v13 is done by:

1. stopping the stack and taking a verified backup (`backup` CLI);
2. `DROP TABLE llm_credentials; DROP TABLE llm_entitlements;
   DELETE FROM schema_migrations WHERE version=14;` (v13's fail-closed data
   lifecycle refuses export/deletion while unknown tables exist), or restoring the
   pre-v14 backup taken before the deploy if nothing after it must be kept;
3. starting the v13 image with the `SEOS_LLM_*` variables left **empty** (v13 would
   otherwise serve every account from one operator key).

Users' saved AI keys are lost by step 2 and must be re-entered after a roll-forward.
