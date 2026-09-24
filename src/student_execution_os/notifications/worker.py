from __future__ import annotations

import argparse
import json
import time
from uuid import uuid4

from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository

from .policy import FCMChannel, NotificationWorker


def run_once(database: str, worker_id: str) -> dict[str, object]:
    delivered = 0
    accounts = 0
    with SQLiteCanonicalRepository(database) as repository:
        repository.initialize()
        channel = FCMChannel(canonical=repository)
        account_ids = [row[0] for row in repository.connection.execute("SELECT id FROM accounts ORDER BY id")]
        worker = NotificationWorker(repository, channel.send)
        for account_id in account_ids:
            accounts += 1
            delivered += worker.run_once(account_id, worker_id)
    return {
        "worker_id": worker_id,
        "accounts": accounts,
        "delivered": delivered,
        "fcm": "CONFIGURED" if channel.config.configured else "UNCONFIGURED",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Durable Student Execution OS notification worker")
    parser.add_argument("--database", required=True)
    parser.add_argument("--worker-id", default=f"notification-worker-{uuid4()}")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    args = parser.parse_args()
    if args.poll_seconds <= 0:
        parser.error("--poll-seconds must be positive")
    while True:
        print(json.dumps(run_once(args.database, args.worker_id), sort_keys=True), flush=True)
        if args.once:
            return 0
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
