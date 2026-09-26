#!/usr/bin/env python3
"""Prepare and optionally serve a test-only Android update source.

The generated Ed25519 key exists only under the requested output directory. Use
``/tmp`` and delete it after testing; it is deliberately unrelated to production.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from student_execution_os.updates.signing import load_private_key, private_seed_b64, public_key_b64  # noqa: E402


def test_key(output: Path) -> tuple[Ed25519PrivateKey, Path]:
    key_file = output / ".test-update-private-key"
    if key_file.is_file():
        key = load_private_key(key_file.read_bytes())
    else:
        key = Ed25519PrivateKey.generate()
        key_file.write_text(private_seed_b64(key), encoding="ascii")
        key_file.chmod(0o600)
    return key, key_file


def print_configuration(args, key: Ed25519PrivateKey, key_file: Path) -> None:
    key_id = "local-dev-ephemeral"
    trust = json.dumps({key_id: public_key_b64(key)}, separators=(",", ":"))
    template = f"http://{args.host_for_device}:{args.port}/{{channel}}/policy.json"
    print("\nTest-only build configuration:")
    print(f"SEOS_UPDATE_POLICY_URL_TEMPLATE='{template}'")
    print(f"SEOS_UPDATE_TRUST_KEYS_JSON='{trust}'")
    print(f"Test private key: {key_file} (delete the output directory after use)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", help="final APK whose versionName/versionCode match the target")
    parser.add_argument("--version")
    parser.add_argument("--build-number", type=int)
    parser.add_argument("--output", required=True, help="test directory, preferably below /tmp")
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--host-for-device", default="10.0.2.2", choices=("10.0.2.2", "127.0.0.1", "localhost"))
    parser.add_argument("--init-only", action="store_true", help="create/reuse the test key and print build configuration")
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()

    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    key, key_file = test_key(output)
    print_configuration(args, key, key_file)
    if args.init_only:
        return 0
    if not args.artifact or not args.version or args.build_number is None:
        raise SystemExit("--artifact, --version and --build-number are required unless --init-only is used")
    artifact = Path(args.artifact).resolve()
    if not artifact.is_file():
        raise SystemExit(f"artifact not found: {artifact}")
    stable = output / "stable"
    stable.mkdir(parents=True, exist_ok=True)
    target = stable / f"student-execution-os-{args.version}-android-universal.apk"
    shutil.copy2(artifact, target)

    key_id = "local-dev-ephemeral"
    env = {**os.environ, "SEOS_UPDATE_SIGNING_KEY_B64": private_seed_b64(key)}
    url = f"http://{args.host_for_device}:{args.port}/stable/{target.name}"
    command = [
        sys.executable, str(ROOT / "tools/update_policy.py"), "create",
        "--artifact", str(target), "--artifact-url", url, "--output", str(stable / "policy.json"),
        "--version", args.version, "--build-number", str(args.build_number), "--sequence", "1",
        "--channel", "STABLE", "--rollout", "100", "--key-id", key_id,
        "--summary-en", "Local update test", "--summary-ru", "Локальная проверка обновления",
        "--change-en", "Verifies download, integrity and PackageInstaller flow",
        "--change-ru", "Проверяет скачивание, целостность и PackageInstaller",
    ]
    subprocess.run(command, env=env, cwd=ROOT, check=True)
    if args.prepare_only:
        return 0
    os.chdir(output)
    print(f"Serving {output} on 0.0.0.0:{args.port}; press Ctrl-C to stop")
    ThreadingHTTPServer(("0.0.0.0", args.port), SimpleHTTPRequestHandler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
