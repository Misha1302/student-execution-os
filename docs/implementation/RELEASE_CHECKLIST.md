# Release checklist and release-candidate gates

This file is the **single canonical list** of release gates. `RC_LEDGER.md` records
implementation-time evidence only. Live evidence for a frozen release is an external,
immutable GitHub Release record as described in **Immutable live evidence** below; any
summary (PR text, chat report) that disagrees with this list is wrong.

## Status definitions

| Status | Means | Requires |
|---|---|---|
| **CLOSED-BETA READY** | The code at a given SHA may be deployed by the owner to invited testers. Nothing is claimed about production behaviour. | Gates **G1–G4** all VERIFIED on that SHA. |
| **RELEASE-CANDIDATE READY** | That SHA has been proven in production-equivalent conditions and could be offered to all students. | **G1–G13** all VERIFIED on the *same* SHA in a published immutable `rc-evidence-<release-sha>` GitHub Release. |

A gate is **VERIFIED** only with recorded evidence for the named SHA/tree. "Tested on the
PR head" counts for a merge commit only when `git rev-parse <merge>^{tree}` equals the tested
head's tree, or the checks re-ran on the merge commit itself.

Evidence labels: MOCKED · LOCAL · CI · PRODUCTION-LIKE · LIVE.

## Gates

### Automated / local (no production authority needed)

| # | Gate | How it is proven |
|---|---|---|
| G1 | CI green on the release SHA itself | `ci/verify` (static, unit/integration, API, Chromium incl. `final_student_e2e`, smokes) and `android/apk` + `android/device` green on the push to `main` for that SHA. |
| G2 | Local gate on the release tree | `make static test api smoke browser` on a clean checkout of the SHA (browser needs `CHROMIUM_PATH` in containers without Playwright's pinned build). |
| G3 | The deploy image builds from the release tree and serves it | `docker build -f deploy/Dockerfile` from the SHA; API + reminder-worker containers with `SEOS_REVISION=<sha>`; `deploy/smoke.py --expect-revision <sha> --expect-worker` green; `backup` + `restore` CLI inside the container → integrity ok, sha256 verified. |
| G4 | Release APK path is exercised | `android/apk`: release build with a throwaway key **and a synthetic Firebase config**, `release_evidence.py --require-fcm --require-clean` (one signer, expected version, push compiled in, credential scan, provenance record). `android/device`: notification buttons E2E, instrumented tests, install/upgrade/downgrade/foreign-key checks on an emulator. |

### Owner-only (production authority; each *mutation* needs its own go-ahead right before it)

| # | Gate | Evidence to record in the external RC evidence bundle |
|---|---|---|
| G5 | Every credential that ever appeared in a chat, ticket or log is rotated (VPS SSH password included; key-based SSH only) *(mutation)* | Date + list of rotated credential *names* (never values). |
| G6 | Secrets live only in secret files | `docker compose ... config` shows paths, never values *(read-only)*; relay `RELAY_TOKEN` matches the API secret file. |
| G7 | Pre-deploy production backup + restore drill *(backup = mutation of the backup dir only)* | Backup manifest (`integrity_check: ok`, schema, account_count), copy stored off-host, restore drill to a scratch path: `sha256_verified: true`, `integrity_check: ok`, `foreign_key_violations: 0`. |
| G8 | Deploy of the release SHA with revision provenance *(mutation)* | `SEOS_REVISION=<sha>` exported; `docker compose ... images --format json` (image IDs); `smoke.py https://<domain> --expect-revision <sha> --expect-worker --expect-push --expect-byok` all ok. |
| G9 | Live Groq | `llm-smoke` → `"result": "OK"` with `route`, `model`, `credential`. Anything else = NOT VERIFIED (the local-parser fallback is never AI success). |
| G10 | Live ChatGPT / Codex | `https://<domain>/mcp` added as a connector; consent screen shows the expected redirect host; one read + one write visible in the app; revocation stops access. |
| G11 | Production-signed APK | `android-release.yml` run for the SHA: `provenance.json` (commit, tree, APK sha256, signing certificate SHA-256, `fcm_config: true`) attached to the `v<version>` release; certificate SHA-256 equals the owner's recorded release key. |
| G12 | Real phone | That APK installed **over the previous production-signed build**: data kept; update prompt from the signed policy; a real FCM push reminder arrives (worker `push_configured: true`, delivery row `SENT`, notification seen) and its buttons act on the server. |
| G13 | Live HSE calendar | A consenting student's real HSE `.ics` feed (or a sanitized current copy) imports: classes appear with correct local times, a re-import is idempotent, a personal note survives it. |

## Immutable live evidence

Committing G5–G13 results would change the source SHA and invalidate the same-revision
claim. The source tree therefore defines only the schema and procedure. Live results are
written outside the Git worktree, every gate repeats the frozen `release_sha`, and the
completed record is published as an immutable GitHub Release asset.

Repository setting **Release immutability** must be enabled before evidence collection.
Use a draft so every safe asset is present before publication; publication locks the tag
and assets and creates GitHub's release attestation. Never publish a partial bundle.

```bash
RELEASE_SHA=$(git rev-parse origin/main)
RELEASE_TREE=$(git rev-parse "$RELEASE_SHA^{tree}")
EVIDENCE_DIR=$(mktemp -d)
python tools/rc_evidence.py new --release-sha "$RELEASE_SHA" \
  --release-tree "$RELEASE_TREE" --operator <operator> \
  --output "$EVIDENCE_DIR/rc-evidence.json"

# Fill the JSON as G1-G13 run. Store only non-secret results, identities, hashes and links.
python tools/rc_evidence.py validate "$EVIDENCE_DIR/rc-evidence.json" \
  --expect-sha "$RELEASE_SHA" --expect-tree "$RELEASE_TREE" --require-all-verified
sha256sum "$EVIDENCE_DIR/rc-evidence.json" >"$EVIDENCE_DIR/rc-evidence.sha256"

TAG="rc-evidence-$RELEASE_SHA"
gh release create "$TAG" --draft --prerelease --target "$RELEASE_SHA" \
  --title "RC evidence $RELEASE_SHA" --notes "Immutable G1-G13 evidence for $RELEASE_SHA"
gh release upload "$TAG" "$EVIDENCE_DIR/rc-evidence.json" \
  "$EVIDENCE_DIR/rc-evidence.sha256"
gh release edit "$TAG" --draft=false

# The record is valid only when the API reports immutable=true and target_commitish is
# RELEASE_SHA. Asset digests and GitHub's release attestation make it replayable.
gh api "repos/{owner}/{repo}/releases/tags/$TAG" \
  --jq '{tag_name,target_commitish,immutable,assets:[.assets[]|{name,digest,size}]}'
```

`tools/rc_evidence.py` rejects missing/duplicate gates, mixed SHAs, incomplete publication,
secret-bearing field names and common secret shapes. This is defense in depth, not a
substitute for operator review: never include passwords, tokens, API keys, private feed
URLs, private keys, or unredacted command output.

## Commands for owner-only gates

```bash
# G6 (read-only; prints variable NAMES only, never values). Expected output: nothing.
docker compose -f deploy/docker-compose.yml --env-file deploy/.env config --format json | python3 -c '
import json, sys
inline = ("SEOS_PLATFORM_LLM_API_KEY", "SEOS_FCM_SERVICE_ACCOUNT_JSON", "SEOS_LLM_EGRESS_PROXY")
for name, svc in json.load(sys.stdin)["services"].items():
    for var, value in (svc.get("environment") or {}).items():
        if var in inline and value:
            print(f"{name}: {var} is set inline; move it to a secret file")'

# G7
docker compose -f deploy/docker-compose.yml exec api \
  python -m student_execution_os backup --database /data/student-execution-os.db \
  --output /data/seos-backup-$(date +%F).db
docker compose -f deploy/docker-compose.yml exec api \
  python -m student_execution_os restore --backup /data/seos-backup-$(date +%F).db --output /tmp/drill.db

# G8 (release-SHA checkout, see deploy/README.md#release-sha-checkout-with-persistent-shared-platform-secrets)
export SEOS_REVISION=<release-sha>
docker compose -f deploy/docker-compose.yml --env-file deploy/.env up -d --build api reminder-worker
docker compose -f deploy/docker-compose.yml --env-file deploy/.env images --format json
python deploy/smoke.py https://<domain> --expect-revision "$SEOS_REVISION" \
  --expect-worker --expect-push --expect-byok

# G9
docker compose -f deploy/docker-compose.yml exec api python -m student_execution_os llm-smoke

# G11: Actions → android-release → Run workflow (from the release SHA); required:
#   secrets SEOS_ANDROID_KEYSTORE_B64, SEOS_KEYSTORE_PASSWORD, SEOS_KEY_PASSWORD, SEOS_KEY_ALIAS,
#           SEOS_GOOGLE_SERVICES_JSON_B64, SEOS_UPDATE_SIGNING_KEY_B64
#   vars    SEOS_SERVER_URL, SEOS_UPDATE_POLICY_URL_TEMPLATE, SEOS_UPDATE_TRUST_KEYS_JSON,
#           SEOS_UPDATE_SIGNING_KEY_ID
# then compare provenance.json → signing.signers[0].certificate_sha256 with the owner's key:
keytool -list -v -keystore <release.jks> -alias <alias> | grep 'SHA256:'
```

## Provenance chain

| Link | Server | Android |
|---|---|---|
| Source | release SHA + `git rev-parse <sha>^{tree}` | same; `provenance.json.source` (build fails on tracked changes) |
| Build | `docker compose ... up -d --build` on the host from that checkout (no registry; the image is built where it runs) | `android-release.yml` `PACKAGE` job at `github.sha` |
| Artifact | local image ID from `docker compose ... images` | APK sha256 + size in `provenance.json` and `artifact.sha256`; re-downloaded from the release and byte-compared |
| Identity | `SEOS_REVISION` on `/api/v1/health`, checked by `smoke.py --expect-revision` | signing certificate SHA-256 in `provenance.json`; update policy signed with `SEOS_UPDATE_SIGNING_KEY_ID`, verified after publish |
| Deploy | `smoke.py` against the domain | GitHub release `v<version>` (immutable) → `updates-<channel>` policy published last |

## Rollback plan

- Previous SHA and image ID recorded (G8). Code rollback = redeploy the previous SHA with its
  `SEOS_REVISION`.
- Schema rollback only via `persistence/rollback/` scripts on a *copy*, and only if they do
  not refuse (v23 source state, v24 connections, v25 live grants, v27 groups refuse while data
  would be lost); otherwise restore the G7 backup (loses writes since the backup; announce first).
- Android: publish a new signed policy with `PAUSED`/`WITHDRAWN` (`update-release-control.yml`);
  a downgrade is refused by Android, so a fix ships as a higher versionCode.
