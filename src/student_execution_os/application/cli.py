from __future__ import annotations

import argparse
import json
from collections.abc import Sequence

from student_execution_os import __version__


def health_payload() -> dict[str, str]:
    """Return the minimal executable-surface health contract for Pass 0."""
    return {
        "api_version": "0",
        "service": "student-execution-os",
        "status": "ok",
        "version": __version__,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="student-execution-os",
        description="Student Execution OS command-line entrypoint.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("health", help="Print a machine-readable smoke/health payload.")
    subparsers.add_parser("version", help="Print the application version.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "health":
        print(json.dumps(health_payload(), sort_keys=True))
        return 0
    if args.command == "version":
        print(__version__)
        return 0
    raise AssertionError(f"Unhandled command: {args.command}")
