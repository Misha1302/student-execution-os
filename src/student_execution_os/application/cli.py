from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone

from student_execution_os import __version__
from student_execution_os.domain.model import (
    ActorCategory,
    HardCutoff,
    Importance,
    ObligationCategory,
)
from student_execution_os.persistence import SQLiteCanonicalRepository
from student_execution_os.planning import FeasibilityEngine, SQLitePlanningStateSource, build_planning_snapshot


def health_payload() -> dict[str, str]:
    return {
        "api_version": "0",
        "service": "student-execution-os",
        "status": "ok",
        "version": __version__,
    }


def run_domain_smoke(database: str) -> dict[str, object]:
    """Exercise the real Pass 1 domain/persistence/concurrency path."""
    account_id = "smoke-account"
    now = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    with SQLiteCanonicalRepository(database) as repo:
        repo.initialize()
        repo.create_account(account_id)
        task = repo.create_task(
            account_id=account_id,
            title="Pass 1 smoke task",
            category=ObligationCategory.GENERAL,
            importance=Importance.NORMAL,
            estimated_total_effort_minutes=60,
            remaining_effort_minutes=60,
            splittable=True,
            min_chunk_minutes=30,
            max_chunk_minutes=60,
            actionable_from=now,
            target_at=now + timedelta(hours=6),
            actual_cutoff=HardCutoff.known(now + timedelta(days=1)),
            actor=ActorCategory.SYSTEM,
        )
        task = repo.update_task(
            account_id=account_id,
            obligation_id=task.obligation.id,
            expected_version=task.obligation.version,
            remaining_effort_minutes=30,
            actor=ActorCategory.SYSTEM,
        )
        return {
            "account_id": account_id,
            "cutoff_state": task.actual_cutoff.state.value,
            "schema_version": repo.schema_version(),
            "server_revision": repo.get_server_revision(account_id),
            "status": "ok",
            "task_version": task.obligation.version,
        }


def run_feasibility_smoke() -> dict[str, object]:
    """Exercise repository -> immutable snapshot -> sound feasibility witness."""
    account_id = "feasibility-smoke-account"
    now = datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc)
    with SQLiteCanonicalRepository(":memory:") as repo:
        repo.initialize()
        repo.create_account(account_id)
        repo.create_task(
            account_id=account_id, obligation_id="smoke-task", title="Pass 2 smoke task",
            category=ObligationCategory.GENERAL, importance=Importance.NORMAL,
            estimated_total_effort_minutes=60, remaining_effort_minutes=60, splittable=True,
            min_chunk_minutes=30, max_chunk_minutes=60, actionable_from=now, target_at=None,
            actual_cutoff=HardCutoff.known(now + timedelta(hours=2)), actor=ActorCategory.SYSTEM,
        )
        snapshot = build_planning_snapshot(
            SQLitePlanningStateSource(repo), account_id=account_id, analysis_horizon_start=now,
            analysis_horizon_end=now + timedelta(hours=2),
            plan_output_horizon_end=now + timedelta(hours=1),
        )
        result = FeasibilityEngine().evaluate(snapshot)
        return {
            "status": result.status.value,
            "input_hash": snapshot.input_hash,
            "server_revision": snapshot.input_server_revision,
            "witness_blocks": len(result.witness),
            "analysis_horizon_end": snapshot.analysis_horizon_end.isoformat(),
            "display_horizon_end": snapshot.plan_output_horizon_end.isoformat(),
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="student-execution-os",
        description="Student Execution OS command-line entrypoint.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("health", help="Print a machine-readable smoke/health payload.")
    subparsers.add_parser("version", help="Print the application version.")
    domain_smoke = subparsers.add_parser(
        "domain-smoke", help="Exercise Pass 1 canonical-domain persistence in SQLite."
    )
    domain_smoke.add_argument("--database", default=":memory:")
    subparsers.add_parser("feasibility-smoke", help="Exercise Pass 2 snapshot and feasibility core.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "health":
        print(json.dumps(health_payload(), sort_keys=True))
        return 0
    if args.command == "version":
        print(__version__)
        return 0
    if args.command == "domain-smoke":
        print(json.dumps(run_domain_smoke(args.database), sort_keys=True))
        return 0
    if args.command == "feasibility-smoke":
        print(json.dumps(run_feasibility_smoke(), sort_keys=True))
        return 0
    raise AssertionError(f"Unhandled command: {args.command}")
