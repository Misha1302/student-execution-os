"""Reminder worker process: runs the reminder engine and the push dispatcher.

    python -m student_execution_os.reminders.worker --database /data/student-execution-os.db

The engine evaluates every open task each ``--tick-seconds``; the dispatcher drains
the push outbox every ``--dispatch-seconds``. Both are safe to restart at any
point: engine decisions are recorded atomically with their message, and a message
leased by a crashed worker is reclaimed after its lease expires.
"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import time
from contextlib import closing
from datetime import datetime, timezone
from uuid import uuid4

from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository

from student_execution_os.reliability.retention import purge_expired

from .engine import ReminderEngine
from .push import PushDispatcher, provider_from_environment

log = logging.getLogger("student_execution_os.reminders.worker")
PURGE_SECONDS = 3600.0


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def heartbeat(database: str, payload: dict) -> None:
    with SQLiteCanonicalRepository(database) as repo:
        repo.initialize()
        with repo._tx() as conn:
            conn.execute(
                "INSERT INTO worker_heartbeats(name,beat_at,detail_json) VALUES ('reminder-worker',?,?) "
                "ON CONFLICT(name) DO UPDATE SET beat_at=excluded.beat_at,detail_json=excluded.detail_json",
                (_now().isoformat(), json.dumps(payload, sort_keys=True)),
            )


def heartbeat_is_fresh(database: str, max_age_seconds: float = 180.0) -> bool:
    """Container health check: the worker loop wrote a heartbeat recently."""
    import sqlite3

    try:
        # sqlite3's own context manager only commits; closing() actually closes the file.
        with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=5)) as conn:
            row = conn.execute("SELECT beat_at FROM worker_heartbeats WHERE name='reminder-worker'").fetchone()
    except sqlite3.Error:
        return False
    if row is None:
        return False
    return (_now() - datetime.fromisoformat(row[0])).total_seconds() < max_age_seconds


def main() -> int:
    parser = argparse.ArgumentParser(description="Student Execution OS reminder worker")
    parser.add_argument("--database", required=True)
    parser.add_argument("--worker-id", default=f"reminder-worker-{uuid4().hex[:8]}")
    parser.add_argument("--tick-seconds", type=float, default=60.0)
    parser.add_argument("--dispatch-seconds", type=float, default=5.0)
    parser.add_argument("--once", action="store_true", help="one engine tick and one dispatch, then exit")
    parser.add_argument("--check-heartbeat", action="store_true", help="exit 0 if the worker heartbeat is fresh")
    args = parser.parse_args()
    if args.check_heartbeat:
        return 0 if heartbeat_is_fresh(args.database) else 1
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    provider = provider_from_environment()
    engine = ReminderEngine(args.database)
    dispatcher = PushDispatcher(args.database, provider)
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log.info("reminder worker %s started; push provider=%s configured=%s",
             args.worker_id, provider.name, provider.configured)
    next_tick = 0.0
    next_purge = 0.0
    while not stopping:
        started = time.monotonic()
        summary: dict = {"worker_id": args.worker_id, "push_provider": provider.name,
                         "push_configured": provider.configured}
        try:
            if started >= next_tick:
                results = engine.tick_all(_now())
                summary["accounts"] = len(results)
                summary["messages"] = sum(len(r.messages) for r in results)
                next_tick = started + args.tick_seconds
            summary["dispatch"] = dispatcher.run_once(_now(), args.worker_id)
            if started >= next_purge:
                # Expired assistant input and old operation logs are deleted, not kept.
                summary["purged"] = purge_expired(args.database, _now())
                next_purge = started + PURGE_SECONDS
            heartbeat(args.database, summary)
            if summary.get("messages") or any(summary["dispatch"].values()) or any(summary.get("purged", {}).values()):
                log.info(json.dumps(summary, sort_keys=True))
        except Exception:
            log.exception("reminder worker iteration failed")
        if args.once:
            print(json.dumps(summary, sort_keys=True), flush=True)
            return 0
        time.sleep(max(0.5, args.dispatch_seconds - (time.monotonic() - started)))
    log.info("reminder worker %s stopped", args.worker_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
