# Operating Android updates

## Trust provisioning

Generate the production Ed25519 key in an approved secret-management environment.
Store its raw 32-byte seed as base64 in the protected GitHub environment secret
`SEOS_UPDATE_SIGNING_KEY_B64`. Put only the public map in repository/environment
variable `SEOS_UPDATE_TRUST_KEYS_JSON`, for example:

```json
{"release-2026":"BASE64_32_BYTE_PUBLIC_KEY"}
```

The Android release signing keystore remains separate and is supplied through
`SEOS_ANDROID_KEYSTORE_B64`, `SEOS_KEY_ALIAS`, `SEOS_KEYSTORE_PASSWORD`, and
`SEOS_KEY_PASSWORD`. Every APK update must use the same signing lineage.

Set `SEOS_UPDATE_POLICY_URL_TEMPLATE` to a public HTTPS template containing
`{channel}`. With the included GitHub workflow it is:

```text
https://raw.githubusercontent.com/OWNER/REPOSITORY/update-{channel}/policy.json
```

The repository/releases must be publicly readable. Never add a GitHub PAT to the
client. Configure protected `android-release` and `update-production` environments
with required reviewers; fork PR workflows do not run release jobs or receive these
secrets.

## Release and incident operation

Run **android-release** manually with a unique SemVer, monotonically increasing
Android build number and policy sequence. The jobs test, package/sign, publish the
immutable versioned APK, re-download and verify it, then sign and publish channel
policy last to the dedicated `update-stable` / `update-beta` metadata branch. The
branch is mutable discovery state; trust still comes exclusively from the Ed25519
signature and monotonically increasing sequence embedded in `policy.json`. Start stable rollouts at 5% unless an explicit decision says otherwise.

Run **update-release-control** to move rollout 5 → 25 → 50 → 100, or set status to
`PAUSED`/`WITHDRAWN`. Every control operation must use a sequence larger than the
current policy. A bad release is paused and followed by a fixed higher version;
installed clients are not downgraded.

`update-policy-refresh` runs daily. Inside the 72-hour safety window it authenticates
the current predecessor (schema plus configured Ed25519 key), increments `sequence`,
renews the seven-day validity window without changing target/status/rollout/mandatory
metadata, publishes with a compare-and-swap content SHA, then verifies both GitHub API
and public raw bytes. Expiry is a client freshness failure, not a publisher authenticity
failure: `verify-authenticity` accepts an expired but correctly signed predecessor;
`verify-client-policy` never does. Invalid signatures, unknown keys and malformed
metadata fail both paths.

All three channel writers use the same `update-channel-<CHANNEL>` concurrency group.
The release preflight authenticates existing channels, enforces global `versionCode`
monotonicity and stable SemVer progression, validates signer/provenance, and generates
and verifies the candidate policy before creating a release. If an earlier run already
created the immutable release, a retry resumes promotion only after exact tag, source
SHA, immutability, asset set, APK hash/size and byte-identical provenance comparison.
In practice that means re-running the failed jobs of the same workflow run; a fresh
dispatch rebuilds the APK, gets different provenance and fails closed (publish a new
SemVer instead). Existing bytes are never uploaded again. Every policy writer reads the
predecessor bytes and blob SHA from one GitHub API response, so its PUT is a true
compare-and-swap against exactly the predecessor it authenticated.

The protocol does not support same-SemVer binary revisions: both stable SemVer and
Android `versionCode` must increase. `minimum_api_version` is not part of the signed
schema because it had no stable runtime semantic owner. Reintroducing it requires an
explicit client/server protocol-negotiation contract.

For suspected signing-key compromise, stop promotion, protect hosting credentials,
and follow two-phase key rotation: first ship a client trusting old+new public keys,
then sign with the new key after adoption. Do not remotely introduce a new trust root
using only a suspected key.

## Manual recovery for 0.7.1 / 0.7.2

Those binaries may fail before handing the APK to Android. Download the immutable APK
from the official GitHub Release for the newest known-good higher `versionCode` and
install it over the existing `io.github.misha1302.seos` application. Do **not**
uninstall: Android's same-package/same-signer upgrade retains application data. Android
must reject a foreign signer and a lower `versionCode`; never work around either
protection and never use an unsigned replacement installer.

## Local 1.0.0 → 1.0.1 test

Create the temporary trust root first. Keep the two printed environment values;
both APKs must embed the same test public key and local policy URL:

```bash
python tools/dev_update_source.py --output /tmp/seos-update-test --init-only
```

In one shell, export those printed values, build the old APK, and keep it outside
the Gradle output directory. Then build the newer APK with a larger versionCode:

```bash
export SEOS_UPDATE_POLICY_URL_TEMPLATE='http://10.0.2.2:8099/{channel}/policy.json'
export SEOS_UPDATE_TRUST_KEYS_JSON='{...printed test-only public key...}'
SEOS_VERSION_NAME=1.0.0 SEOS_VERSION_CODE=100 make apk
cp mobile/android/app/build/outputs/apk/debug/app-debug.apk /tmp/seos-update-test/app-1.0.0.apk
SEOS_VERSION_NAME=1.0.1 SEOS_VERSION_CODE=101 make apk
```

Now prepare and serve policy plus the final 1.0.1 bytes. The helper reuses the
test key created by `--init-only`:

```bash
python tools/dev_update_source.py \
  --artifact mobile/android/app/build/outputs/apk/debug/app-debug.apk \
  --version 1.0.1 --build-number 101 --output /tmp/seos-update-test
```

Install `/tmp/seos-update-test/app-1.0.0.apk`, create user data, launch it, check
and apply the update through Settings, accept Android's confirmation, then assert
version 1.0.1 and retained data. The emulator reaches the host at `10.0.2.2`.
The generated private key remains under `/tmp/seos-update-test`; delete that
directory afterwards. A debug build has no production endpoint/trust key by default.
