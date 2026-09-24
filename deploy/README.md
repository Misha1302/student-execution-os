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
EOF
chmod 0600 /etc/student-execution-os/student-execution-os.env

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

## Not yet covered

Password reset/change, email verification, per-account rate limits across several server
processes (the limiter is in-process), push notifications to the phone. See
ADR 0015 for the full boundary.
