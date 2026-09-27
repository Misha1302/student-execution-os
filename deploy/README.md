# Deploying the server

The container runs the API and web client in **session mode**: people register and log in,
and every request is bound to the account of its bearer session. Caddy terminates TLS and
obtains a certificate automatically for your domain. On a host that already runs nginx,
use the separate nginx deployment below; it does not start Caddy or claim public ports.

## First start

1. Point a DNS record (A/AAAA) for your domain at the host; open ports 80 and 443.
2. `cp deploy/.env.example deploy/.env` and set `SEOS_DOMAIN`.
3. `docker compose -f deploy/docker-compose.yml --env-file deploy/.env up -d --build`
4. Open `https://<domain>/` in a browser or enter the same address in the Android app.

After you and the people you invite have registered, close registration:
set `SEOS_REGISTRATION=closed` in `deploy/.env` and run the `up -d` command again.

The compose deployment also starts the durable reminder worker. Push remains disabled
until the FCM service account is installed (see **Secrets**) and the Android app contains
a matching `google-services.json`. Never commit either credential. The worker evaluates
execution state separately from its leased technical delivery retries.

## Secrets

Two secrets exist, each as a file in a directory mounted read-only into exactly the one
container that needs it (files, not environment variables: variables show up in
`docker inspect`, `/proc/*/environ` and `docker compose config`).

| File | Container | Purpose |
|---|---|---|
| `secrets/worker/fcm-service-account.json` | reminder-worker | Firebase Admin SDK service account for FCM push |
| `secrets/api/credential.key` | api | master key that encrypts every account's own AI key (ADR 0017) |
| `secrets/api/platform-groq-1.key` | api | primary platform LLM credential (never copied into `.env`) |
| `secrets/api/platform-groq-2.key` | api | standby platform LLM credential (never copied into `.env`) |
| `secrets/api/llm-egress-proxy.url` | api | optional HTTP(S) proxy URL for LLM-only egress; needed only when a provider rejects the VPS network |
| `secrets/api/llm-egress-relay.token` | api | optional shared secret for the Cloudflare Groq relay (same value as the Worker's `RELAY_TOKEN`) |

For the Docker + Caddy variant the directory is `deploy/secrets/` (git-ignored); for the
nginx variant it is `/etc/student-execution-os/secrets/`. The container user is uid 10001:

```bash
S=/etc/student-execution-os/secrets            # or deploy/secrets
install -d -m 0755 "$S"
install -d -o 10001 -g 10001 -m 0700 "$S/api" "$S/worker"
install -o 10001 -g 10001 -m 0400 /path/to/firebase-adminsdk.json "$S/worker/fcm-service-account.json"
# The key never appears on a terminal: the command writes the file (mode 0600).
docker run --rm -u 10001 -v "$S/api:/k" student-execution-os:release \
  python -m student_execution_os credential-key-generate --output /k/credential.key
chmod 0400 "$S/api/credential.key" && chmod 0500 "$S/api" "$S/worker"
```

Back up `credential.key` separately from database backups (a database backup alone must
not be enough to read users' AI keys). Losing it only means users re-enter their keys.
Rotate with `credential-key-generate --rotate`, restart, then
`python -m student_execution_os credentials-rekey --database /data/student-execution-os.db`
in the api container, and finally delete the old (second) line.

Verify push credentials end to end without showing anything on a phone:

```bash
docker compose ... exec reminder-worker python -m student_execution_os.reminders.worker \
  --database /data/student-execution-os.db --check-push --check-push-devices
```

(OAuth token exchange, FCM `validate_only` send for project permission, and a
validate-only check of every registered device token.)

## AI (LLM) access

Resolution is `USER_BYOK > PLATFORM_MANAGED STARTER > deterministic local parser`.
Each user may add an OpenAI, Anthropic, or OpenAI-compatible key in Settings → AI; it
is stored encrypted per account and never shown again. With
`SEOS_STARTER_LLM_ENABLED=1`, registration and safe startup backfill grant STARTER to
accounts that have no existing entitlement. Turning the flag off disables STARTER even
when platform keys exist.

Use `SEOS_PLATFORM_LLM_API_KEY_FILES` for primary/standby server-side credentials;
files take precedence over backward-compatible `SEOS_PLATFORM_LLM_API_KEY`. Standby is
tried once only after credential `AUTH`/`QUOTA`, never after 429, 5xx, network, model,
request, or format failures. The keys are not stored in SQLite, exports, clients,
logs, diagnostics, or exception text. The old `SEOS_LLM_*` variables remain ignored.

`agent/usage.py::StarterQuotaPolicy` owns all STARTER limits. Defaults are a 30-day
period, 100 requests/100,000 tokens per account, and global hard caps of 10,000
requests/10,000,000 tokens. A conservative maximum is atomically reserved before the
outbound request and reconciled from provider usage afterward. Exhaustion or provider
failure returns the local parser, not HTTP 500. BYOK bypasses this ledger entirely.
STARTER does not implement paid billing.

### Provider rejects the server but the key works elsewhere

BYOK inference deliberately originates from the API container so the saved key is never
returned to browser or Android storage. Therefore a successful `curl` from a laptop/VPN
does **not** prove that the provider accepts the VPS egress IP. If Settings shows
`SERVER_BLOCKED` (HTTP 403) while the same key works from another network, keep the key
server-side and change only the network path of allowlisted LLM requests.

The application already has host-scoped egress seams, so changing network origin stays a
deployment concern rather than adding provider-specific transport branches. The options
below are deliberately explicit: Tor + Privoxy is the free CONNECT-proxy overlay, a
dedicated HTTP CONNECT proxy preserves end-to-end provider TLS, and the Cloudflare Groq
relay is the current production default for Groq. Native SOCKS5/SOCKS5h support would add
application dependencies without providing a capability the existing HTTP CONNECT seam
lacks.

#### Free Tor overlay

The optional `deploy/docker-compose.tor.yml` overlay adds:

```
Execution OS API
    |  HTTP CONNECT, only for exact hosts in SEOS_LLM_EGRESS_PROXY_HOSTS
    v
Privoxy (internal Docker network only)
    |  SOCKS5t; provider DNS resolution happens through Tor
    v
Tor
    |
    v
HTTPS AI provider
```

Privoxy and Tor publish **no host ports**. Privoxy is attached only to the internal
`llm-egress` network, while Tor is dual-homed to that network and a separate
`tor-uplink` network for Internet access. The API keeps its normal `default` network,
so all non-LLM traffic and non-allowlisted provider hosts continue to use the ordinary
route. HTTPS still terminates at the AI provider; neither Privoxy nor Tor performs TLS
interception.

For the nginx deployment, set the exact provider hosts in the existing environment file:

```bash
SEOS_LLM_EGRESS_PROXY_HOSTS=api.groq.com
```

Then start with both Compose files:

```bash
docker compose \
  -p student-execution-os \
  -f deploy/docker-compose.nginx.yml \
  -f deploy/docker-compose.tor.yml \
  --env-file /etc/student-execution-os/student-execution-os.env \
  up -d --build
```

The overlay sets `SEOS_LLM_EGRESS_PROXY=http://llm-egress-proxy:8118` itself, so do
not put that internal Docker hostname into the host environment. It also clears
`SEOS_LLM_EGRESS_PROXY_FILE` to prevent an external proxy secret from being combined
with the Tor route. If `SEOS_LLM_EGRESS_PROXY_HOSTS` is empty or unset, the overlay
defaults it to `api.groq.com`; use a comma-separated exact-host list to add other
operator-approved AI providers.

For the Caddy deployment the same overlay is composed over `deploy/docker-compose.yml`:

```bash
docker compose \
  -f deploy/docker-compose.yml \
  -f deploy/docker-compose.tor.yml \
  --env-file deploy/.env \
  up -d --build
```

Tor's healthcheck reports healthy only after its control interface says bootstrap
progress reached 100%. Privoxy's healthcheck uses its internal status URL and consumes
no provider API key. API startup is not gated on Tor becoming healthy: a temporary Tor
failure must degrade only proxied AI requests rather than take down the rest of the app.

A Tor exit can itself be rejected by an AI provider. Treat that as an operational
`SERVER_BLOCKED` outcome: record the provider's status/reason without secrets and do
not automatically rotate Tor identity or retry indefinitely.

**One-step nginx rollback:** run the normal deployment command without the Tor overlay;
`--remove-orphans` also removes the Tor/Privoxy containers:

```bash
docker compose \
  -p student-execution-os \
  -f deploy/docker-compose.nginx.yml \
  --env-file /etc/student-execution-os/student-execution-os.env \
  up -d --build --remove-orphans
```

The base Compose file supplies empty proxy settings, so recreating the API this way
returns LLM traffic to its previous direct route.

#### External HTTP(S) egress proxy

The existing host-scoped proxy seam also supports a dedicated HTTP CONNECT egress host.
This is useful when the production VPS address is rejected but a separate
provider-supported address is accepted. It remains an explicit operator choice alongside
the Tor overlay and the Cloudflare Groq relay below.

A hardened Squid bootstrap is included at
`deploy/llm-egress/squid/setup-egress-host.sh`. On a fresh Ubuntu/Debian egress VM:

```bash
sudo sh deploy/llm-egress/squid/setup-egress-host.sh \
  <SEOS_PRODUCTION_PUBLIC_IPV4> api.groq.com
```

The script accepts exactly one production IPv4 source, installs a persistent host-firewall
chain before Squid, binds Squid to IPv4 port 3128, allows only `CONNECT` to port 443, and
accepts only exact DNS hostnames (no wildcard or suffix ACLs). It does not cache or
intercept TLS. Re-running it replaces only the dedicated `SEOS_SQUID` firewall chain and
the managed Squid configuration.

Also restrict the cloud security group / security list for TCP 3128 to the same production
`/32`; do not expose the proxy to `0.0.0.0/0`. Verify from the production host without
using an API key:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' \
  --proxy http://PROXY_IP:3128 https://api.groq.com/
curl -sS -o /dev/null -w '%{http_code}\n' \
  --proxy http://PROXY_IP:3128 https://example.com/
```

Any HTTP response from the allowlisted provider proves the CONNECT tunnel reached it;
the non-allowlisted destination must fail at the proxy.

To use the proxy from Student Execution OS:

1. Put `http://PROXY_IP:3128` in the API-only secret file
   `secrets/api/llm-egress-proxy.url` (or the nginx deployment equivalent under
   `/etc/student-execution-os/secrets/api/`) and keep it mode 0400.
2. Set
   `SEOS_LLM_EGRESS_PROXY_FILE=/run/secrets/seos/llm-egress-proxy.url` and
   `SEOS_LLM_EGRESS_PROXY_HOSTS=api.groq.com` in the deployment environment.
3. Clear the relay settings and deploy without the Tor overlay so only one egress
   mechanism owns the provider host.
4. Recreate the API container and run **Settings → AI → Test** again.

For a proxy URL without credentials, `SEOS_LLM_EGRESS_PROXY=http://proxy:3128` may be
used instead of the file. Configure exactly one of the two proxy sources. The application
uses the proxy only when the provider request hostname exactly matches the comma-separated
allowlist; arbitrary user-supplied OpenAI-compatible hosts continue to use the normal
route.

#### Cloudflare Groq relay (default for production)

`deploy/cloudflare-groq-relay/` is a Cloudflare Worker that relays exactly
`POST /openai/v1/chat/completions` to the hard-coded
`https://api.groq.com/openai/v1/chat/completions`. The API sends there **only** requests
whose original host is exactly in `SEOS_LLM_EGRESS_RELAY_HOSTS`:

```
Execution OS API --HTTPS, X-SEOS-Relay-Token + user's Authorization--> Worker --HTTPS--> Groq
Execution OS API --everything else--> Internet directly
```

**Trust difference:** unlike Tor or a CONNECT proxy, the Worker **terminates TLS**, so
Cloudflare can technically observe the user's Groq key and the prompt it forwards. The
Worker code never logs, stores or returns them (Workers observability is disabled), and
forwards the key only to the hard-coded Groq endpoint. It is not end-to-end TLS between
the API and Groq. The relay is scoped by exact host; arbitrary user-supplied
OpenAI-compatible addresses never reach it. Redirects are never followed and TLS
verification stays on at both hops. Deploying the Worker: see its `README.md`.

Enable it (nginx deployment):

```bash
S=/etc/student-execution-os/secrets
# write the same value that was stored as the Worker secret RELAY_TOKEN, without echoing it
install -o 10001 -g 10001 -m 0400 /dev/stdin "$S/api/llm-egress-relay.token" < /path/to/relay-token
```

and in `/etc/student-execution-os/student-execution-os.env`:

```bash
SEOS_LLM_EGRESS_RELAY_URL=https://seos-groq-relay.misha13022008.workers.dev
SEOS_LLM_EGRESS_RELAY_TOKEN_FILE=/run/secrets/seos/llm-egress-relay.token
SEOS_LLM_EGRESS_RELAY_HOSTS=api.groq.com
SEOS_LLM_EGRESS_PROXY=
SEOS_LLM_EGRESS_PROXY_FILE=
SEOS_LLM_EGRESS_PROXY_HOSTS=
```

then run the normal deployment command **without** the Tor overlay (`--remove-orphans`
removes Tor/Privoxy). Only one egress mechanism may serve a host: a request that both a
proxy and the relay would handle fails with `REQUEST` ("configure only one LLM egress
mechanism") instead of silently picking one. The Tor overlay clears the relay settings
itself, so enabling it is the explicit switch back to Tor.

Errors the Worker produces carry `X-SEOS-Relay-Error` and are never reported as the
user's key: `unauthorized`/`invalid_request`/`relay_misconfigured` → `REQUEST` (operator
configuration), `provider_unreachable` → `NETWORK`, `upstream_redirect` → `UPSTREAM`.
Answers from Groq itself have no such header and keep the normal classification (401 →
`AUTH`, region 403 → `SERVER_BLOCKED`, model 403 → `NOT_FOUND`, 429 → `RATE_LIMITED`/`QUOTA`).

**Rollback A (direct):** empty the three `SEOS_LLM_EGRESS_RELAY_*` variables and run the
normal nginx deployment command above with `--remove-orphans`.
**Rollback B (Tor):** run the Tor overlay command above; it disables the relay. There is
no automatic Worker→Tor failover by design: switching network origin is an operator decision.

Reminder pushes to current Android builds are data-only and rendered by the app with
working Start / Done / Snooze buttons; older installs still get system-rendered pushes.
The worker also deletes expired Assistant previews (the text people typed or dictated)
every hour and trims operation logs / the reminder inbox after 90 days.

## Data and backups

SQLite lives in the `seos-data` volume (`/data/student-execution-os.db`). Take a
consistent, verified backup with the existing CLI:

```bash
docker compose -f deploy/docker-compose.yml exec api \
  python -m student_execution_os backup --database /data/student-execution-os.db \
  --output /data/seos-backup-$(date +%F).db
```

Copy the backup and its `.manifest.json` off the host. The backup contains login password
hashes and session token hashes (never raw passwords or tokens); treat it as sensitive.
User-facing account export (Settings → Export) never includes credentials.

## Existing nginx host (`seos.185-102-139-43.sslip.io`)

The nginx variant publishes the application only on host loopback and stores data in a
host directory. It is intended for the current VPS, whose nginx owns ports 80 and 443.

```bash
install -d -m 0755 /opt/student-execution-os/releases /etc/student-execution-os
install -d -o 10001 -g 10001 -m 0700 \
  /var/lib/student-execution-os /var/backups/student-execution-os
cat >/etc/student-execution-os/student-execution-os.env <<'EOF'
SEOS_REGISTRATION=open
SEOS_PROXY_HEADERS=1
SEOS_FORWARDED_ALLOW_IPS=127.0.0.1
SEOS_CORS_ORIGINS=
SEOS_IMAGE_TAG=release
SEOS_PLATFORM_LLM_PROVIDER=
SEOS_PLATFORM_LLM_MODEL=
SEOS_PLATFORM_LLM_BASE_URL=
SEOS_PLATFORM_LLM_API_KEY_FILES=
SEOS_PLATFORM_LLM_API_KEY=
SEOS_STARTER_LLM_ENABLED=0
EOF
chmod 0600 /etc/student-execution-os/student-execution-os.env
# then install the FCM service account and the credential master key (see "Secrets")

docker compose -p student-execution-os -f deploy/docker-compose.nginx.yml \
  --env-file /etc/student-execution-os/student-execution-os.env up -d --build
```

Install `deploy/nginx/student-execution-os.conf` as the nginx site, obtain/maintain its
certificate with Certbot's nginx integration, and run `nginx -t` before reloading nginx.
The public URL is `https://seos.185-102-139-43.sslip.io`; health is at
`/api/v1/health`, auth at `/api/v1/auth/*`, and all other account API calls require the
bearer session token returned by registration/login.

### Release-SHA checkout with persistent shared platform secrets

For `~/seos-staging/<release-sha>/deploy/docker-compose.yml`, keep secrets outside the
release so the next checkout reuses the same read-only mount. Do not put key contents
in the environment or repository. Run these commands on the host after the two key
files have already been installed by the operator:

```bash
cd ~/seos-staging/<release-sha>
test -r ~/seos-staging/shared/secrets/api/platform-groq-1.key
test -r ~/seos-staging/shared/secrets/api/platform-groq-2.key
sudo chown 10001:10001 ~/seos-staging/shared/secrets/api \
  ~/seos-staging/shared/secrets/api/platform-groq-1.key \
  ~/seos-staging/shared/secrets/api/platform-groq-2.key
sudo chmod 0700 ~/seos-staging/shared/secrets/api
sudo chmod 0400 ~/seos-staging/shared/secrets/api/platform-groq-1.key \
  ~/seos-staging/shared/secrets/api/platform-groq-2.key
```

Set or replace these exact entries in `deploy/.env` (do not append duplicate names):

```dotenv
SEOS_API_SECRETS_DIR=../../shared/secrets/api
SEOS_STARTER_LLM_ENABLED=1
SEOS_PLATFORM_LLM_PROVIDER=openai-compatible
SEOS_PLATFORM_LLM_MODEL=openai/gpt-oss-20b
SEOS_PLATFORM_LLM_BASE_URL=https://api.groq.com/openai/v1
SEOS_PLATFORM_LLM_API_KEY_FILES=/run/secrets/seos/platform-groq-1.key,/run/secrets/seos/platform-groq-2.key
SEOS_STARTER_LLM_PERIOD_SECONDS=2592000
SEOS_STARTER_LLM_ACCOUNT_REQUEST_LIMIT=100
SEOS_STARTER_LLM_ACCOUNT_TOKEN_LIMIT=100000
SEOS_STARTER_LLM_GLOBAL_REQUEST_LIMIT=10000
SEOS_STARTER_LLM_GLOBAL_TOKEN_LIMIT=10000000
SEOS_STARTER_LLM_MAX_OUTPUT_TOKENS=1200
SEOS_STARTER_LLM_TOKEN_RESERVATION_OVERHEAD=256
```

Then validate and recreate only the API service:

```bash
docker compose -f deploy/docker-compose.yml --env-file deploy/.env config
docker compose -f deploy/docker-compose.yml --env-file deploy/.env up -d --build api
docker compose -f deploy/docker-compose.yml --env-file deploy/.env exec api \
  python -m student_execution_os --help
curl --fail --silent --show-error https://<domain>/api/v1/health
```

The two `test -r` commands inspect only file presence/readability, not contents. Review
`docker compose ... config` before `up`; it may show file paths but must never show the
key contents. A normal login can then verify Settings → AI shows Basic AI, the model,
remaining quota, and reset time.

Rollback STARTER without deleting data: set `SEOS_STARTER_LLM_ENABLED=0`, clear both
`SEOS_PLATFORM_LLM_API_KEY_FILES` and `SEOS_PLATFORM_LLM_API_KEY`, and recreate `api`;
STARTER becomes inactive and capture falls back to BYOK/local parsing. For a schema rollback, stop all writers, take
a verified backup, drop the three v21 usage tables, delete migration 21, optionally
delete only `llm_entitlements WHERE plan='STARTER'`, then deploy the previous image.

For daily verified backups with 14-day retention, install the two files from
`deploy/systemd/` under `/etc/systemd/system/`, enable
`student-execution-os-backup.timer`, and run the service once immediately. Backups and
their verification manifests are written to `/var/backups/student-execution-os`.

## Without Docker

```bash
python -m pip install -r requirements.txt
PYTHONPATH=src python -m student_execution_os.web.server \
  --database /var/lib/seos/seos.db --host 127.0.0.1 --port 8765 \
  --proxy-headers --forwarded-allow-ips 127.0.0.1
```

and put any TLS reverse proxy (nginx, Caddy) in front of `127.0.0.1:8765`.
All flags can also be set through environment variables: `SEOS_DATABASE`, `SEOS_HOST`,
`SEOS_PORT`, `SEOS_REGISTRATION`, `SEOS_CORS_ORIGINS`, `SEOS_PROXY_HEADERS=1`,
`SEOS_FORWARDED_ALLOW_IPS`.

Run the worker as a separate process when deploying without Compose:

```bash
PYTHONPATH=src python -m student_execution_os.reminders.worker \
  --database /var/lib/seos/seos.db
```

## Post-deploy smoke

```bash
python deploy/smoke.py https://<domain> --expect-revision "$(git rev-parse HEAD)" \
  --expect-worker --expect-push --expect-byok
```

registers a throwaway account, checks that a natural-language phrase is previewed and stored
with every field and that snooze schedules a reminder, drives create → start → progress →
complete → open completed → reopen → reuse through `/api/v1/sync` (including an exactly-once replay and a
visible lifecycle conflict), checks reminders, Assistant capabilities/preview and the
reminder-worker heartbeat (and, with `--expect-push`, that the worker has FCM configured).
`--expect-byok` saves a deliberately invalid AI key, checks that it is masked and absent
from every response, that the real provider rejects it (`INVALID_KEY`) and that capture
falls back to the local parser, then removes it. The account is deleted at the end.

## Not yet covered

See [docs/ROADMAP.md](../docs/ROADMAP.md): password reset/change, email verification,
per-account rate limits across several server processes (the limiter is in-process),
routing, OAuth, automated FCM credential rotation, and paid/managed AI. Local simulation
is not presented as production delivery.
