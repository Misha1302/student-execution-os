# ADR 0020 — Signed policy and Android sideload updates

- Status: Accepted
- Date: 2026-09-26

## Actual platform scope

The repository has no native desktop application. It contains a FastAPI-hosted
browser client and a real Capacitor/Android application. The browser client is
not a PWA: it has no service worker or installed application shell. Consequently
this slice implements the binary updater only for Android and does not add a fake
desktop/Velopack project or a fake PWA updater.

The ordinary browser always receives static assets from the deployed server. Its
deployment remains an atomic server/CDN concern. A future installable desktop or
PWA must add its own `IPlatformUpdateAdapter` equivalent without changing policy
ownership.

## Ownership

The ES-module `AppUpdateService` is the canonical application owner. It depends on:

- `StaticUpdatePolicyProvider`: fetches an independent, public policy URL; no API
  session or GitHub credential is sent;
- the typed policy/evaluator in `update-domain.js`: selects one exact target;
- `AndroidUpdateAdapter`: the project-owned Capacitor boundary;
- local Capacitor Preferences: channel, anonymous installation id, maximum
  accepted sequence, pending update and startup health.

`SeosUpdatePlugin` performs mechanics only. It cannot choose `latest`: it receives
the exact version/build/hash selected from signed policy. It downloads to a private
`.partial` file, bounds size, rejects insecure release URLs and redirects, checks
disk space, SHA-256, package/version/build and APK signing identity, atomically
renames the verified cache file, and verifies all fields again immediately before
creating a `PackageInstaller` session.

Android is always allowed to require user confirmation. Unknown-source permission
is handled through the OS settings screen; there is no silent-install bypass.

## Trust and publication

Ed25519 signs canonical JSON containing policy, routing, rollout, mandatory and
artifact fields. Android API 24–32 lacks platform Ed25519, so the official Tink
Android library supplies verification across the existing minSdk. Public keys are
injected into a signed APK as a key-id map. The release private key exists only in
the protected promotion environment.

Versioned GitHub Release assets are immutable byte hosting. A mutable
`updates-stable` or `updates-beta` release tag hosts only the signed `policy.json`.
It is not a trust root: tampering, expiry and lower sequences are rejected. Release
promotion uploads and re-downloads the APK first, verifies its hash, signs policy,
and replaces the channel policy last. Pause/withdraw changes only signed metadata
with a larger sequence; it never downgrades installed clients.

## Data and migrations

APK replacement does not touch Android app data. Capacitor Preferences, WebView
storage, cached reads and the offline mutation queue stay inside the application
data sandbox. The server database is a separate deployment at `/data` and is never
an update artifact. Server schema migrations still run in the server lifecycle,
not in Android `PackageInstaller`; release policy declares rollback compatibility
and defaults to `BINARY_ONLY`, so the client never advertises automatic binary
downgrade across an unknown schema boundary.

## Recovery and health

The client writes a pending version before handing the package to Android. The new
version increments launch attempts, then marks itself healthy only after the normal
UI has rendered. Three incomplete critical startups retain diagnostics and do not
create a restart loop. Recovery is forward-fix/reinstall by default; automatic
downgrade is absent.
