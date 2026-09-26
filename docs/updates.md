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
https://github.com/OWNER/REPOSITORY/releases/download/updates-{channel}/policy.json
```

The repository/releases must be publicly readable. Never add a GitHub PAT to the
client. Configure protected `android-release` and `update-production` environments
with required reviewers; fork PR workflows do not run release jobs or receive these
secrets.

## Release and incident operation

Run **android-release** manually with a unique SemVer, monotonically increasing
Android build number and policy sequence. The jobs test, package/sign, publish the
immutable versioned APK, re-download and verify it, then sign and publish channel
policy last. Start stable rollouts at 5% unless an explicit decision says otherwise.

Run **update-release-control** to move rollout 5 → 25 → 50 → 100, or set status to
`PAUSED`/`WITHDRAWN`. Every control operation must use a sequence larger than the
current policy. A bad release is paused and followed by a fixed higher version;
installed clients are not downgraded.

For suspected signing-key compromise, stop promotion, protect hosting credentials,
and follow two-phase key rotation: first ship a client trusting old+new public keys,
then sign with the new key after adoption. Do not remotely introduce a new trust root
using only a suspected key.

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
