# Release checklist (release candidate)

Each line is either checked by automation (CI / `make` target) or is an operator step that
needs production authority. Operator steps are **production mutations** unless marked
read-only; each needs its own go-ahead immediately before it is performed.

## 1. Code gate (automated)

- [ ] `main` CI green on the release SHA: `verify` (static, unit/integration/api, smoke,
      browser incl. `final_student_e2e`) and `android` (`apk`, `device`).
- [ ] `make static test api smoke browser` green locally on the release SHA.
- [ ] `docs/implementation/RC_LEDGER.md` has no stage in a state other than MERGED, and
      every known limitation is listed in its section.

## 2. Secrets (operator)

- [ ] Rotate every credential that has ever appeared in a chat, ticket or log (the VPS SSH
      password included); use key-based SSH only. *(mutation)*
- [ ] Platform Groq keys (primary + standby), credential master key, FCM service account,
      relay token live only in the secret files described in `deploy/README.md#secrets`;
      `docker compose ... config` shows paths, never values. *(read-only check)*
- [ ] The Cloudflare relay `RELAY_TOKEN` matches the API secret file (set without echo).

## 3. Backup before deploy (operator)

```bash
docker compose -f deploy/docker-compose.yml exec api \
  python -m student_execution_os backup --database /data/student-execution-os.db \
  --output /data/seos-backup-$(date +%F).db
```

- [ ] Manifest reports `integrity_check: ok`; backup + manifest copied off the host.
- [ ] Restore drill on a scratch path (never over the live file):
      `python -m student_execution_os restore --backup <file> --output /tmp/drill.db`
      → sha256 verified, integrity ok. *(read-only for production data)*

## 4. Deploy (operator, mutation)

- [ ] Release-SHA checkout, `docker compose ... config`, `up -d --build api reminder-worker`
      (`deploy/README.md#release-sha-checkout-with-persistent-shared-platform-secrets`).
- [ ] Migrations run on start (v22…v27 are additive; rollback scripts in
      `persistence/rollback/`, several fail closed while they would lose user data).

## 5. Post-deploy verification

- [ ] `python deploy/smoke.py https://<domain> --expect-revision <sha> --expect-worker
      --expect-push --expect-byok` → all green. *(creates and deletes a throwaway account)*
- [ ] `docker compose ... exec api python -m student_execution_os llm-smoke` → `"result":
      "OK"` with the expected `route`/`credential`. Anything else = Groq NOT verified; the
      app keeps working on the local parser, which must not be reported as AI success.
- [ ] ChatGPT/Codex: add `https://<domain>/mcp` as a connector, complete consent, run one
      read and one write (`docs/integrations/chatgpt-codex.md`).
- [ ] Android: install the signed release APK over the previous one on a real device;
      data kept; the update prompt and a push reminder arrive.

## 6. Rollback plan

- [ ] Previous image tag recorded. Code rollback = redeploy the previous SHA; schema rollback
      only via the documented down scripts on a copy, and only if they don't refuse; else
      restore the pre-deploy backup (loses writes since the backup — announce first).
