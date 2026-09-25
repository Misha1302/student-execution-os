from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import uvicorn

from student_execution_os.persistence import SQLiteCanonicalRepository

from .app import create_app
from .auth import DEFAULT_CORS_ORIGINS, AuthConfig

_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def build_parser() -> argparse.ArgumentParser:
    env = os.environ.get
    parser = argparse.ArgumentParser(
        description=(
            "Run the Student Execution OS web/API host. Without --account the server runs in "
            "session mode: users register/log in and every request is bound to their session."
        )
    )
    parser.add_argument("--database", default=env("SEOS_DATABASE", "student-execution-os.db"))
    parser.add_argument(
        "--account",
        default=None,
        help="Bound mode: serve exactly this account without login (loopback only). Omit for session mode.",
    )
    parser.add_argument("--principal", default="local-user")
    parser.add_argument("--host", default=env("SEOS_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(env("SEOS_PORT", "8765")))
    parser.add_argument("--init-account", action="store_true")
    parser.add_argument(
        "--allow-non-loopback",
        action="store_true",
        help="Deprecated escape hatch for bound mode. Bound mode has no authentication; prefer session mode.",
    )
    parser.add_argument(
        "--registration",
        choices=("open", "closed"),
        default=env("SEOS_REGISTRATION", "open"),
        help="Session mode: whether new users may register.",
    )
    parser.add_argument(
        "--cors-origin",
        action="append",
        default=[o for o in env("SEOS_CORS_ORIGINS", "").split(",") if o.strip()],
        help="Extra allowed CORS origin (repeatable). Capacitor app origins are always allowed.",
    )
    parser.add_argument(
        "--proxy-headers",
        action="store_true",
        default=env("SEOS_PROXY_HEADERS", "") == "1",
        help="Trust X-Forwarded-* headers from --forwarded-allow-ips (run behind a TLS reverse proxy).",
    )
    parser.add_argument("--forwarded-allow-ips", default=env("SEOS_FORWARDED_ALLOW_IPS", "127.0.0.1"))
    return parser


def _report_ai_configuration() -> None:
    """Say at startup how AI keys are handled; never print a key."""
    from student_execution_os.agent.credentials import CredentialCipher

    cipher = CredentialCipher.from_environment()
    state = "enabled" if cipher else "DISABLED (no SEOS_CREDENTIAL_KEY_FILE): users cannot save AI keys"
    print(f"student-execution-os: per-account AI keys {state}", file=sys.stderr, flush=True)
    legacy = [name for name in ("SEOS_LLM_PROVIDER", "SEOS_LLM_API_KEY", "SEOS_LLM_MODEL", "SEOS_LLM_BASE_URL")
              if os.environ.get(name)]
    if legacy:
        print("student-execution-os: ignoring " + ", ".join(legacy) + "; AI is per-account (Settings -> AI). "
              "Operator credentials for entitled accounts use SEOS_PLATFORM_LLM_*.", file=sys.stderr, flush=True)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    database = Path(args.database)
    with SQLiteCanonicalRepository(database) as repo:
        repo.initialize()
        if args.account is not None:
            if args.init_account:
                repo.create_account(args.account)
            repo._require_account(args.account)
    if args.account is not None:
        if args.host not in _LOOPBACK and not args.allow_non_loopback:
            raise SystemExit("Refusing non-loopback bind in bound (no-login) mode; omit --account for session mode")
        app = create_app(database, account_id=args.account, principal_id=args.principal)
    else:
        app = create_app(
            database,
            auth=AuthConfig(
                registration_open=args.registration == "open",
                cors_origins=tuple(dict.fromkeys([*DEFAULT_CORS_ORIGINS, *(o.strip() for o in args.cors_origin)])),
            ),
        )
    _report_ai_configuration()
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="info",
        proxy_headers=args.proxy_headers,
        forwarded_allow_ips=args.forwarded_allow_ips if args.proxy_headers else None,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
