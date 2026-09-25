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

AI is **bring your own key**: each user adds an OpenAI, Anthropic or OpenAI-compatible
key in Settings → AI; it is stored encrypted per account and never shown again. Without a
key the app uses its built-in parser. The operator pays for nobody's inference.

`SEOS_PLATFORM_LLM_PROVIDER/MODEL/API_KEY/BASE_URL` configure operator credentials for a
future paid tier; they are used **only** for accounts granted an entitlement
(`python -m student_execution_os llm-entitlement --database … --login <user> --grant <plan>`).
The old `SEOS_LLM_*` variables are ignored (the server warns at startup).

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
SEOS_PLATFORM_LLM_API_KEY=
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
