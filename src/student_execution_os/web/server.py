from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from student_execution_os.persistence import SQLiteCanonicalRepository

from .app import create_app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the local Student Execution OS web UI.")
    parser.add_argument("--database", default="student-execution-os.db")
    parser.add_argument("--account", required=True, help="Server-bound account id; never supplied by browser requests.")
    parser.add_argument("--principal", default="local-user")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--init-account", action="store_true")
    parser.add_argument(
        "--allow-non-loopback",
        action="store_true",
        help="Explicitly permit binding beyond loopback. This pre-release server has no production authentication transport.",
    )
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.host not in {"127.0.0.1", "localhost", "::1"} and not args.allow_non_loopback:
        raise SystemExit("Refusing non-loopback bind without --allow-non-loopback")
    database = Path(args.database)
    with SQLiteCanonicalRepository(database) as repo:
        repo.initialize()
        if args.init_account:
            repo.create_account(args.account)
        repo._require_account(args.account)
    app = create_app(database, account_id=args.account, principal_id=args.principal)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
