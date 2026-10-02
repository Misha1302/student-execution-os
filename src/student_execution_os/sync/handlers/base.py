"""Shared base of the sync command handlers."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable

from student_execution_os.domain.errors import ValidationError
from student_execution_os.domain.model import ActorCategory
from student_execution_os.persistence import extras
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository
from student_execution_os.execution import SQLiteExecutionStore

from ..serialize import task_payload

from ..primitives import APPLIED, Outcome

Handler = Callable[[str, dict[str, Any]], Outcome]


class CommandHandler:
    """Context and shared primitives of the domain command handlers.

    A handler owns the semantics of its operation types only. It runs inside the
    caller's transaction: SyncService owns the envelope, op_id replay, request
    hashing, the savepoint and result persistence, so no handler repeats them.
    """

    def __init__(self, repo: SQLiteCanonicalRepository, *, account_id: str, actor: ActorCategory,
                 now: datetime) -> None:
        self.repo = repo
        self.account_id = account_id
        self.actor = actor
        self.now = now

    def _task(self, task_id: str):
        return self.repo.get_task(self.account_id, task_id)

    def _execution(self) -> SQLiteExecutionStore:
        return SQLiteExecutionStore(self.repo)

    def _execution_moment(self, payload: dict[str, Any]) -> datetime:
        raw = payload.get("occurred_at")
        if raw in (None, ""):
            return self.now
        try:
            moment = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError("occurred_at must be an ISO-8601 instant") from exc
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValidationError("occurred_at must include a UTC offset")
        # This is a user-reported execution fact, not server conflict ordering.
        if moment > self.now + timedelta(minutes=5):
            raise ValidationError("occurred_at is too far in the future")
        return moment

    def _task_out(self, task_id: str, status: str = APPLIED, code: str | None = None, message: str | None = None) -> Outcome:
        from student_execution_os.reminders.store import ReminderStore
        remind = ReminderStore(self.repo).remind_at(self.account_id, task_id)
        count = extras.progress_counts_for(self.repo, self.account_id, [task_id]).get(task_id)
        return Outcome(status, task_payload(self._task(task_id), remind_at=remind, count=count), code, message)

    def _touch(self, task_id: str, *, snooze_until: datetime | None = None, remind_at: datetime | None = None) -> None:
        from student_execution_os.reminders.store import ReminderStore
        ReminderStore(self.repo).touch(self.account_id, task_id, self.now, snooze_until=snooze_until, remind_at=remind_at)

    def _update(self, task_id: str, **fields) -> None:
        current = self._task(task_id)
        self.repo.update_task(account_id=self.account_id, obligation_id=task_id,
                              expected_version=current.obligation.version, actor=self.actor, **fields)

    def _transition(self, obligation_id: str, action: str) -> None:
        ob = self.repo.get_obligation(self.account_id, obligation_id)
        method = {"complete": self.repo.complete_obligation, "cancel": self.repo.cancel_obligation,
                  "reopen": self.repo.reopen_obligation, "archive": self.repo.archive_obligation,
                  "unarchive": self.repo.unarchive_obligation}[action]
        method(account_id=self.account_id, obligation_id=obligation_id, expected_version=ob.version, actor=self.actor)

    def _capture_actor(self, payload: dict[str, Any]) -> ActorCategory:
        """A task confirmed from an Assistant preview keeps its LLM provenance.

        The reference must name a live preview batch of this account; otherwise (for
        example an offline capture replayed after the batch expired) the confirmed
        values are simply the user's own input.
        """
        batch_id = payload.get("assistant_batch_id")
        if not batch_id:
            return self.actor
        row = self.repo.connection.execute(
            "SELECT expires_at FROM assistant_batches WHERE account_id=? AND id=?", (self.account_id, str(batch_id))
        ).fetchone()
        if row is None or datetime.fromisoformat(row["expires_at"]) <= self.now:
            return self.actor
        return ActorCategory.USER_VIA_LLM

    @staticmethod
    def _local_instant(value: Any, field: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(str(value))
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"{field} must be an ISO local civil datetime") from exc
        if parsed.tzinfo is not None:
            raise ValidationError(f"{field} must not include an offset")
        return parsed.replace(second=0, microsecond=0)
