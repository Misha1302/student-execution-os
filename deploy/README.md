# Deploying the server

The container runs the API and web client in **session mode**: people register and log in,
and every request is bound to the account of its bearer session.

There are two deployment topologies. They do not mix: pick the one that matches the host and
use only its commands. A normal application release never switches between them.

| | Topology A — Caddy host | Topology B — existing nginx host (production) |
|---|---|---|
| Compose | `deploy/docker-compose.yml` | `-p student-execution-os -f deploy/docker-compose.nginx.yml` |
| Environment | `deploy/.env` | `/etc/student-execution-os/student-execution-os.env` (root, 0600) |
| Ports 80/443 | Caddy container (TLS, automatic certificate) | host nginx (`deploy/nginx/`); the app listens on `127.0.0.1:8765` only |
| Database | named volume `seos-data` | `/var/lib/student-execution-os` |
| Backups | inside the volume, copy off the host | `/var/backups/student-execution-os` (mounted at `/backups`) |
| Secrets | `deploy/secrets/` | `/etc/student-execution-os/secrets/` |
| Section | **First start (Topology A)** | **Existing nginx host (Topology B)** |

Never run `deploy/docker-compose.yml` on the nginx host: its Caddy container would compete
with nginx for ports 80/443, and it would start a second, empty database volume.

## First start (Topology A — Caddy host)

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

Secrets are files in directories mounted read-only into only the containers that need
them (files, not environment variables: variables show up in
`docker inspect`, `/proc/*/environ` and `docker compose config`).

| File | Container | Purpose |
|---|---|---|
| `secrets/worker/fcm-service-account.json` | reminder-worker | Firebase Admin SDK service account for FCM push |
| `secrets/api/credential.key` | api | master key that encrypts every account's own AI key (ADR 0017) |
| `secrets/academic/academic-feed.key` | api + reminder-worker | dedicated master key for private iCalendar URLs (ADR 0028); the worker receives no LLM key |
| `secrets/api/platform-groq-1.key` | api | primary platform LLM credential (never copied into `.env`) |
| `secrets/api/platform-groq-2.key` | api | standby platform LLM credential (never copied into `.env`) |
| `secrets/api/llm-egress-proxy.url` | api | optional HTTP(S) proxy URL for LLM-only egress; needed only when a provider rejects the VPS network |
| `secrets/api/llm-egress-relay.token` | api | optional shared secret for the Cloudflare Groq relay (same value as the Worker's `RELAY_TOKEN`) |

For the Docker + Caddy variant the directory is `deploy/secrets/` (git-ignored); for the
nginx variant it is `/etc/student-execution-os/secrets/`. The container user is uid 10001:

```bash
S=/etc/student-execution-os/secrets            # or deploy/secrets
install -d -m 0755 "$S"
install -d -o 10001 -g 10001 -m 0700 "$S/api" "$S/worker" "$S/academic"
install -o 10001 -g 10001 -m 0400 /path/to/firebase-adminsdk.json "$S/worker/fcm-service-account.json"
# The key never appears on a terminal: the command writes the file (mode 0600).
docker run --rm -u 10001 -v "$S/api:/k" student-execution-os:release \
  python -m student_execution_os credential-key-generate --output /k/credential.key
docker run --rm -u 10001 -v "$S/academic:/k" student-execution-os:release \
  python -m student_execution_os credential-key-generate --output /k/academic-feed.key
chmod 0400 "$S/api/credential.key" "$S/academic/academic-feed.key"
chmod 0500 "$S/api" "$S/worker" "$S/academic"
```

Back up `credential.key` separately from database backups (a database backup alone must
not be enough to read users' AI keys). Losing it only means users re-enter their keys.
Rotate with `credential-key-generate --rotate`, restart, then
`python -m student_execution_os credentials-rekey --database /data/student-execution-os.db`
in the api container, and finally delete the old (second) line.

Back up `academic-feed.key` separately as well. Losing it does not damage the last
imported canonical timetable, but automatic refresh remains unavailable until users
reconnect their subscription. Keep an older key as an additional line during rotation;
new connections use the first key and existing connections continue to decrypt with the
older one.

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
STARTER does not implement paid billing. Every attempt counts one request. The token
reservation is released only when the provider certainly generated nothing (HTTP
401/402/403/404/405/413/415/429, or a request refused before leaving the server), so a
`SERVER_BLOCKED` outage does not drain student token budgets; timeouts, network errors
after connect, 5xx and 400 answers stay fully charged (fail closed). Groq 429s
(`rate_limit_exceeded`, even with the billing upsell link) are `RATE_LIMITED`, never
`QUOTA`: no standby failover and no sticky key status. When the provider supplies an
explicit `Retry-After` of at most 30 seconds and the 45-second Assistant operation
budget can still fit another call, the same credential is retried exactly once; otherwise
the request degrades to the local parser and exposes `retry_after_seconds`. Every actual
provider attempt is still counted as one STARTER request; a 429 reconciles with zero
tokens.

Egress is chosen per request and is deterministic: `RELAY` or `PROXY` only for exact
hosts listed by the operator, otherwise `DIRECT`; ambient `HTTPS_PROXY`/`ALL_PROXY`
variables are ignored. A user-supplied API address is `DIRECT_PINNED`: resolved once,
every answer must be public, and the connection goes only to those addresses (TLS and
`Host` keep the name). Each request logs one line on `student_execution_os.llm`:
`llm_request provider=… route=… result=… http_status=… latency_ms=…` — never the URL,
key, relay token, prompt or provider body.

### Provider rejects the server but the key works elsewhere

BYOK inference deliberately originates from the API container so the saved key is never
returned to browser or Android storage. Therefore a successful `curl` from a laptop/VPN
does **not** prove that the provider accepts the VPS egress IP. If Settings shows
`SERVER_BLOCKED` (HTTP 403) while the same key works from another network, keep the key
server-side and change only the network path of allowlisted LLM requests.

The application already has host-scoped egress seams, so changing network origin stays a
deployment concern rather than adding provider-specific transport branches. The options
below are deliberately explicit: the Cloudflare WARP proxy is the production route for
Groq, Tor + Privoxy is the free CONNECT-proxy overlay, a dedicated HTTP CONNECT proxy
preserves end-to-end provider TLS, and the Cloudflare Groq relay is kept for hosts where
it is accepted. Native SOCKS5/SOCKS5h support would add
application dependencies without providing a capability the existing HTTP CONNECT seam
lacks.

#### Cloudflare WARP egress (production route for Groq)

Groq rejects the production VPS directly, through Tor exits and through the Cloudflare
Worker relay (Cloudflare forwards the caller's country on Worker subrequests): all three
answer `403`. A Cloudflare WARP client in proxy mode is accepted (an invalid-key probe gets
Groq's `401 invalid_api_key`, i.e. the request reached authentication). It runs as its own
Compose project, `deploy/llm-egress/warp/compose.yml`, so an application release or
rollback never stops it:

```
Execution OS API --HTTP CONNECT, only api.groq.com--> seos-groq-warp:40001 (WARP proxy mode) --> Groq
Execution OS API --everything else--> normal route
```

- **Ownership:** the WARP project is host infrastructure owned by the operator, started once
  per host; its WARP registration (a free, anonymous device registration) lives in the
  external volume `seos-warp-state`, never in the repository.
- **Exposure:** no host port is published. The proxy listens on the internal network
  `seos-g9-egress_egress` (no route out except through WARP) and the WARP container's own
  uplink network. Only containers explicitly attached to `seos-g9-egress_egress` can use it,
  and CONNECT keeps TLS end to end: the proxy never sees the provider key or the prompt.
- **Restarts:** `restart: unless-stopped` and Docker enabled at boot bring it back after a
  reboot; its healthcheck reports healthy only when WARP is connected and a request through
  the proxy egresses with `warp=on`. Stop it with `stop`, not `down`: `down` removes the
  network the API is attached to.
- **Failure behaviour:** if WARP is down, only allowlisted provider requests fail (`NETWORK`)
  and capture falls back to local parsing; the API itself is unaffected. There is no
  automatic failover to another route.

Start (once per host) and verify:

```bash
docker volume create seos-warp-state
docker compose -f deploy/llm-egress/warp/compose.yml up -d --build
docker inspect -f '{{.State.Health.Status}}' seos-groq-warp      # healthy
```

Then deploy the application with the overlay, which points `SEOS_LLM_EGRESS_PROXY` at the
WARP proxy for exactly `api.groq.com`, clears the relay and `SEOS_LLM_EGRESS_PROXY_FILE`
(one egress owner), and attaches only the `api` service to `seos-g9-egress_egress`:

```bash
docker compose \
  -p student-execution-os \
  -f deploy/docker-compose.nginx.yml \
  -f deploy/docker-compose.warp-egress.yml \
  --env-file /etc/student-execution-os/student-execution-os.env \
  up -d --build --remove-orphans
docker exec student-execution-os-api-1 python -m student_execution_os llm-smoke   # "result": "OK", "route": "PROXY"
```

Do not combine it with `docker-compose.tor.yml`. **Rollback:** deploy without the overlay
(the environment file's relay or proxy settings apply again); the WARP project can keep
running unused.

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

#### Cloudflare Groq relay

`deploy/cloudflare-groq-relay/` is a Cloudflare Worker that relays exactly
`POST /openai/v1/chat/completions` to the hard-coded
`https://api.groq.com/openai/v1/chat/completions`. The API sends there **only** requests
whose original host is exactly in `SEOS_LLM_EGRESS_RELAY_HOSTS`:

```
Execution OS API --HTTPS, X-SEOS-Relay-Token + user's Authorization--> Worker --HTTPS--> Groq
Execution OS API --everything else--> Internet directly
```

As of 2026-10-02 Groq answers `403` to this relay when it is called from the production
VPS (see the WARP section above); it remains an option where the caller's region is accepted.

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
`AUTH`, region 403 → `SERVER_BLOCKED`, model 403/404/decommissioned → `NOT_FOUND`,
429 → `RATE_LIMITED`, or `QUOTA` only for `insufficient_quota`).

**Rollback A (direct):** empty the three `SEOS_LLM_EGRESS_RELAY_*` variables and run the
normal nginx deployment command above with `--remove-orphans`.
**Rollback B (Tor):** run the Tor overlay command above; it disables the relay. There is
no automatic Worker→Tor failover by design: switching network origin is an operator decision.

Reminder pushes to current Android builds are data-only and rendered by the app with
working Start / Done / Snooze buttons; older installs still get system-rendered pushes.
The worker also deletes expired Assistant previews (the text people typed or dictated)
every hour and trims operation logs / the reminder inbox after 90 days.

## Data and backups

SQLite is `/data/student-execution-os.db` inside the containers. Take a consistent,
verified backup with the existing CLI (it writes `<backup>.manifest.json` with the schema
version, account count, SHA-256 and the integrity-check result):

```bash
# Topology A: the seos-data volume
docker compose -f deploy/docker-compose.yml exec api \
  python -m student_execution_os backup --database /data/student-execution-os.db \
  --output /data/seos-backup-$(date +%F).db

# Topology B: /var/backups/student-execution-os on the host (= /backups in the api container)
sudo /opt/student-execution-os/current/deploy/backup.sh
#   or, with the systemd units installed: sudo systemctl start student-execution-os-backup.service
```

Copy the backup and its `.manifest.json` off the host. The backup contains login password
hashes and session token hashes (never raw passwords or tokens); treat it as sensitive.
User-facing account export (Settings → Export) never includes credentials.

## Existing nginx host (Topology B — `seos.185-102-139-43.sslip.io`)

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

### Releasing an exact revision (Topology B)

Releases are immutable `git archive` trees in `/opt/student-execution-os/releases/<full-sha>`;
`/opt/student-execution-os/current` should point at the running one (the backup unit uses it),
but the source of truth for what runs is the Compose project itself (step 0): the symlink is
only updated by step 6 and can lag behind a release done another way.
The environment file and the secrets stay outside the release, so every release reuses
them; never copy secret values into a release tree, and never print them.

```bash
SHA=<full 40-character release sha>            # e.g. the merged main commit
REL=/opt/student-execution-os/releases/$SHA
ENV=/etc/student-execution-os/student-execution-os.env

# 0. What runs now? Record it: it is the rollback target. Release with the same Compose file
#    set (with or without an egress overlay: docker-compose.warp-egress.yml or
#    docker-compose.tor.yml); a release does not change topology.
docker compose ls --filter name=student-execution-os          # CONFIG FILES = $PREV/deploy/...
docker inspect --format '{{.Config.Image}}' student-execution-os-api-1   # previous image tag
PREV=/opt/student-execution-os/releases/<sha from CONFIG FILES>
curl -fsS https://seos.185-102-139-43.sslip.io/api/v1/health   # previous revision and schema_version

# 1. Release tree from the exact commit (no .git, nothing else in it).
sudo install -d -m 0755 "$REL"
git -C /path/to/clone fetch origin && git -C /path/to/clone archive "$SHA" | sudo tar -x -C "$REL"

# 2. Presence / readability only — never cat these files.
sudo test -r "$ENV"
sudo test -r /etc/student-execution-os/secrets/api/credential.key
sudo test -r /etc/student-execution-os/secrets/academic/academic-feed.key
sudo test -r /etc/student-execution-os/secrets/worker/fcm-service-account.json   # if push is enabled
# platform AI keys, if STARTER is enabled: each path listed in SEOS_PLATFORM_LLM_API_KEY_FILES,
# as seen from the host (default dir /etc/student-execution-os/secrets/api/)

# 3. Verified backup with the RUNNING release, before the new image migrates the schema.
#    (backup.sh uses the compose file under `current`; call the CLI directly so it is the
#    running container regardless of where the symlink points.)
B=/backups/student-execution-os-$(date -u +%Y-%m-%dT%H%M%SZ)-pre-${SHA:0:7}.db
docker exec student-execution-os-api-1 python -m student_execution_os backup \
  --database /data/student-execution-os.db --output "$B"
sudo cat "/var/backups/student-execution-os/$(basename "$B").manifest.json"   # schema_version, sha256, integrity_check "ok"

# 4. Pin the revision for both services: api and reminder-worker read SEOS_REVISION
#    (health "revision"); SEOS_IMAGE_TAG (the full SHA) names the image, so the previous
#    image stays available for a rollback without a build.
sudo cp -p "$ENV" "$ENV.bak-pre-${SHA:0:7}"
sudo sed -i -e "s/^SEOS_REVISION=.*/SEOS_REVISION=$SHA/" -e "s/^SEOS_IMAGE_TAG=.*/SEOS_IMAGE_TAG=$SHA/" "$ENV"
sudo grep -q "^SEOS_REVISION=$SHA$" "$ENV" || echo "SEOS_REVISION=$SHA" | sudo tee -a "$ENV" >/dev/null
sudo grep -q "^SEOS_IMAGE_TAG=$SHA$" "$ENV" || echo "SEOS_IMAGE_TAG=$SHA" | sudo tee -a "$ENV" >/dev/null

# 5. Review the resolved configuration without printing the environment.
cd "$REL"
C="docker compose -p student-execution-os -f deploy/docker-compose.nginx.yml"   # + the egress overlay step 0 showed
#    (-f deploy/docker-compose.warp-egress.yml needs the WARP project healthy first:
#     docker inspect -f '{{.State.Health.Status}}' seos-groq-warp)
sudo $C --env-file "$ENV" config --format json | python3 -c '
import json, sys
c = json.load(sys.stdin)
for name, svc in sorted(c["services"].items()):
    print(name, svc.get("image"), "revision=" + str(svc.get("environment", {}).get("SEOS_REVISION")),
          [p.get("host_ip", "") + ":" + str(p.get("published", "")) for p in svc.get("ports", [])],
          [v.get("source") for v in svc.get("volumes", [])])'
#    expect: api and reminder-worker revision=$SHA, api only on 127.0.0.1:8765, data in
#    /var/lib/student-execution-os, no caddy service.
sudo $C --env-file "$ENV" config --format json | python3 -c '
import json, sys
env = json.load(sys.stdin)["services"]["api"].get("environment", {})
print({k: env.get(k) for k in sorted(env) if k.startswith("SEOS_LLM_EGRESS") and not k.endswith("TOKEN_FILE")})'
#    expect exactly one egress owner per host (proxy hosts and relay hosts do not overlap).

# 6. Build and recreate (migrations run once, when the new api starts).
sudo $C --env-file "$ENV" up -d --build --remove-orphans
sudo ln -sfn "$REL" /opt/student-execution-os/current
docker image ls student-execution-os --format '{{.Tag}} {{.ID}} {{.CreatedAt}}'   # provenance

# 7. Verify.
curl -fsS https://seos.185-102-139-43.sslip.io/api/v1/health    # revision == $SHA, schema_version
python3 deploy/smoke.py https://seos.185-102-139-43.sslip.io --expect-revision "$SHA" --expect-worker
docker exec student-execution-os-api-1 python -m student_execution_os llm-smoke        # STARTER: "result": "OK" (exit 0); prints no key
```

`--remove-orphans` removes services the chosen Compose files no longer define; with the
same file set as step 0 it removes nothing.

#### Platform AI (STARTER) on Topology B

The platform keys are files in the API-only secret directory (default
`/etc/student-execution-os/secrets/api/`, override with `SEOS_API_SECRETS_DIR` in `$ENV`):

```bash
S=/etc/student-execution-os/secrets/api
sudo test -r "$S/platform-groq-1.key" && sudo test -r "$S/platform-groq-2.key"
sudo chown 10001:10001 "$S/platform-groq-1.key" "$S/platform-groq-2.key"
sudo chmod 0400 "$S/platform-groq-1.key" "$S/platform-groq-2.key"
```

and in `$ENV` (replace existing names, do not append duplicates):

```dotenv
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

then recreate with step 6. (`/run/secrets/seos/` is the container path of the API secret
directory.) On a Caddy host (Topology A) the same keys go in `deploy/secrets/api/`, or in a
shared directory named by `SEOS_API_SECRETS_DIR` in `deploy/.env`, and the same entries go in
`deploy/.env`.

Rollback STARTER without deleting data: set `SEOS_STARTER_LLM_ENABLED=0`, clear both
`SEOS_PLATFORM_LLM_API_KEY_FILES` and `SEOS_PLATFORM_LLM_API_KEY`, and recreate `api`;
STARTER becomes inactive and capture falls back to BYOK/local parsing.

### Rollback (Topology B)

**Application rollback (the default).** Every migration so far only adds tables or
columns, and a build starts on a database a newer build has migrated: it skips the
migrations it knows and ignores the rest. So a rollback is the previous release with the
current database:

```bash
cd "$PREV"                                         # recorded in step 0 of the release
sudo cp -p "$ENV.bak-pre-${SHA:0:7}" "$ENV"        # previous SEOS_REVISION / SEOS_IMAGE_TAG
sudo docker compose -p student-execution-os -f deploy/docker-compose.nginx.yml [-f <egress overlay from step 0>] \
  --env-file "$ENV" up -d --remove-orphans          # the previous image tag still exists: no build
sudo ln -sfn "$PREV" /opt/student-execution-os/current
curl -fsS https://seos.185-102-139-43.sslip.io/api/v1/health   # previous revision; schema_version stays
```

Data written by the newer build is kept (and used again on roll-forward, which needs no
migration). While the older build runs, the newer features are inactive (for v30: planning
preferences are kept but not applied), and **account export and account deletion answer
`422 VALIDATION_ERROR` ("data lifecycle contract does not classify database tables")** —
the lifecycle guard refuses to export or delete an account incompletely. Everything else
(sign-in, today/plan, sync with exactly-once replay, reminders, backups) works. Verified for
a v30 database under the v27 and v29 builds; `tests/integration/test_v30_planning_preferences.py`
keeps the contract.

**Schema rollback (only when export/deletion must work on the old build for a long time).**
Stop the api and worker, take a verified backup, then in the api image apply
`src/student_execution_os/persistence/rollback/<NNN>_*_down.sql` for every version newer than
the old build, newest first (v30 → v29: `030`; v30 → v27: `030`, `029`, `028`), and start the
old release. This **deletes** those tables' rows: planning preferences (030), Assistant undo
history (029), sign-in rate-limit windows (028). A later roll-forward recreates them empty.
Never restore an older backup over newer legitimate writes to roll back; `restore` of a
newer-schema backup into an older build is refused by design.

**Android.** Android refuses to install a lower versionCode, so a client rollback is a newer
versionCode built from older source; see `mobile/README.md` ("Storage") for what such a
build finds (pending operations in the WebView mirror; sign-in again).

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
python deploy/smoke.py https://<domain> --expect-revision <release-sha> \
  --expect-worker --expect-push --expect-byok
```

`--expect-revision` passes only if the services were started with that `SEOS_REVISION`
(see "Releasing an exact revision" above). `--expect-push` proves the worker has an
FCM credential configured, not that Google delivered a message to a phone.

registers a throwaway account, checks that a natural-language phrase is previewed and stored
with every field and that snooze schedules a reminder, drives create → start → progress →
complete → open completed → reopen → reuse through `/api/v1/sync` (including an exactly-once replay and a
visible lifecycle conflict), checks reminders, Assistant capabilities/preview and the
reminder-worker heartbeat (and, with `--expect-push`, that the worker has FCM configured).
`--expect-byok` saves a deliberately invalid AI key, checks that it is masked and absent
from every response, that the real provider rejects it (`INVALID_KEY`) and that capture
falls back to the local parser, then removes it. The account is deleted at the end.

### Live platform LLM smoke

Inside the API container (it has the platform key files and egress settings):

```bash
# Topology B
sudo docker compose -p student-execution-os -f deploy/docker-compose.nginx.yml \
  --env-file /etc/student-execution-os/student-execution-os.env exec api python -m student_execution_os llm-smoke
# Topology A
docker compose -f deploy/docker-compose.yml exec api python -m student_execution_os llm-smoke
```

sends one real probe through the configured primary/standby credentials and egress route
and prints only `{"result", "route", "provider", "model", "http_status", "latency_ms",
"total_tokens", "credential"}` (exit 0 = `OK`, 2 = provider/egress failure, 3 = not
configured). It never uses the local parser, so `OK` means the provider returned a typed
action. It spends one small request on the platform key and does not touch STARTER
counters or the database.

## Not yet covered

See [docs/ROADMAP.md](../docs/ROADMAP.md): password reset/change, email verification,
per-account rate limits across several server processes (the limiter is in-process),
routing, automated FCM credential rotation, and paid/managed AI. Local simulation
is not presented as production delivery.
