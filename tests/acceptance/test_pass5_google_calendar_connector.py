from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import unittest

from student_execution_os.connectors import (
    ConnectorHealth,
    ConnectorSessionStatus,
    GoogleCalendarConnector,
    GoogleCalendarInvalidSyncToken,
    GoogleCalendarPage,
    GoogleCalendarTransientError,
    SQLiteConnectorRepository,
)
from student_execution_os.domain.clock import FrozenClock
from student_execution_os.domain.model import (
    ActorCategory,
    HardCutoff,
    Importance,
    LifecycleStatus,
    ObligationCategory,
)
from student_execution_os.persistence import SQLiteCanonicalRepository
from student_execution_os.reconciliation import (
    BindingState,
    SQLiteReconciliationRepository,
    SourceAvailability,
)


UTC = timezone.utc
BASE = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


def event(
    event_id: str,
    updated: str,
    *,
    status: str = "confirmed",
    summary: str = "Lecture",
):
    return {
        "id": event_id,
        "updated": updated,
        "etag": f'"{event_id}:{updated}"',
        "status": status,
        "summary": summary,
        "eventType": "default",
        "start": {"dateTime": "2026-09-21T09:00:00+00:00"},
        "end": {"dateTime": "2026-09-21T10:00:00+00:00"},
    }


class ScriptedTransport:
    def __init__(self, responses):
        self.responses = deque(responses)
        self.calls: list[tuple[str | None, str | None]] = []

    def list_events(self, *, calendar_id, sync_token, page_token):
        self.calls.append((sync_token, page_token))
        if not self.responses:
            raise AssertionError("unexpected provider call")
        response = self.responses.popleft()
        if isinstance(response, BaseException):
            raise response
        return response


class Pass5GoogleCalendarConnectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FrozenClock(BASE)
        self.repo = SQLiteCanonicalRepository(":memory:", clock=self.clock)
        self.repo.initialize()
        self.repo.create_account("a")
        self.recon = SQLiteReconciliationRepository(self.repo)
        self.recon.create_source_system(
            account_id="a",
            source_system_id="gcal-source",
            kind="GOOGLE_CALENDAR",
            policy_context={"provider": "google_calendar"},
            actor=ActorCategory.SYSTEM,
        )
        self.connector_repo = SQLiteConnectorRepository(self.repo, self.recon)

    def tearDown(self) -> None:
        self.repo.close()

    def make_local_task(self, task_id: str = "task"):
        return self.repo.create_task(
            account_id="a",
            obligation_id=task_id,
            title="Keep me",
            category=ObligationCategory.GENERAL,
            importance=Importance.NORMAL,
            estimated_total_effort_minutes=30,
            remaining_effort_minutes=30,
            splittable=False,
            actual_cutoff=HardCutoff.unknown(),
            actor=ActorCategory.USER_UI,
        )

    def bind(self, external_id: str, task_id: str = "task"):
        return self.recon.bind_source_entity(
            account_id="a",
            source_system_id="gcal-source",
            external_entity_id=external_id,
            local_entity_id=task_id,
            match_decision_id=f"match:{external_id}",
            actor=ActorCategory.RECONCILER,
        )

    def connector(self, transport):
        return GoogleCalendarConnector(
            account_id="a",
            connector_id="gcal",
            source_system_id="gcal-source",
            calendar_id="primary",
            transport=transport,
            reconciliation=self.recon,
            connectors=self.connector_repo,
            max_retries=0,
            sleeper=lambda _: None,
        )

    def seed_complete_sync(self, external_id: str = "e1"):
        transport = ScriptedTransport(
            [
                GoogleCalendarPage(
                    items=(event(external_id, "2026-09-20T10:00:00Z"),),
                    next_page_token=None,
                    next_sync_token="token-1",
                )
            ]
        )
        result = self.connector(transport).sync()
        self.assertEqual(result.session.status, ConnectorSessionStatus.COMPLETE)
        self.assertEqual(result.checkpoint, "token-1")

    def test_at38_partial_poll_cannot_infer_deletion_from_absence(self):
        self.make_local_task()
        self.bind("e1")
        self.seed_complete_sync("e1")

        transport = ScriptedTransport(
            [
                GoogleCalendarPage(
                    items=(event("e2", "2026-09-20T11:00:00Z"),),
                    next_page_token="page-2",
                    next_sync_token=None,
                ),
                GoogleCalendarTransientError("network"),
            ]
        )
        result = self.connector(transport).sync()

        self.assertEqual(result.session.status, ConnectorSessionStatus.PARTIAL)
        self.assertEqual(result.checkpoint, "token-1")
        self.assertEqual(result.health, ConnectorHealth.STALE)
        binding = self.recon.get_binding("a", self.bind("e1").id)
        self.assertEqual(binding.state, BindingState.ACTIVE)
        task = self.repo.get_task("a", "task")
        self.assertEqual(task.obligation.lifecycle_status, LifecycleStatus.ACTIVE)
        removals = [
            obs
            for obs in self.recon.list_observations("a")
            if obs.value_type.value == "SOURCE_REMOVED"
        ]
        self.assertEqual(removals, [])
        self.assertEqual(
            self.recon.current_source_availability("a", "gcal-source"),
            SourceAvailability.STALE,
        )

    def test_at39_checkpoint_advances_only_after_complete_durable_range(self):
        self.seed_complete_sync("e1")
        changed = event("e1", "2026-09-20T11:30:00Z", summary="Changed")
        transport = ScriptedTransport(
            [
                GoogleCalendarPage(
                    items=(changed,),
                    next_page_token="page-2",
                    next_sync_token=None,
                ),
                GoogleCalendarTransientError("network"),
            ]
        )
        failed = self.connector(transport).sync()
        self.assertEqual(failed.session.status, ConnectorSessionStatus.PARTIAL)
        self.assertEqual(failed.checkpoint, "token-1")
        source_records_after_partial = self.repo.connection.execute(
            "SELECT count(*) FROM source_records WHERE account_id='a'"
        ).fetchone()[0]

        retry = ScriptedTransport(
            [
                GoogleCalendarPage(
                    items=(changed,),
                    next_page_token="page-2",
                    next_sync_token=None,
                ),
                GoogleCalendarPage(
                    items=(event("e2", "2026-09-20T11:45:00Z"),),
                    next_page_token=None,
                    next_sync_token="token-2",
                ),
            ]
        )
        completed = self.connector(retry).sync()
        self.assertEqual(completed.session.status, ConnectorSessionStatus.COMPLETE)
        self.assertEqual(completed.checkpoint, "token-2")
        self.assertEqual(retry.calls[0], ("token-1", None))
        source_records_after_retry = self.repo.connection.execute(
            "SELECT count(*) FROM source_records WHERE account_id='a'"
        ).fetchone()[0]
        self.assertEqual(
            source_records_after_retry,
            source_records_after_partial + 1,
        )
        duplicate_revision_count = self.repo.connection.execute(
            "SELECT count(*) FROM connector_ingestion_receipts "
            "WHERE account_id='a' AND connector_id='gcal' AND external_entity_id='e1' "
            "AND provider_revision LIKE '2026-09-20T11:30:00Z%'"
        ).fetchone()[0]
        self.assertEqual(duplicate_revision_count, 1)

    def test_at40_invalid_cursor_full_resync_preserves_local_obligation(self):
        self.make_local_task()
        self.bind("e1")
        self.seed_complete_sync("e1")

        transport = ScriptedTransport(
            [
                GoogleCalendarInvalidSyncToken("expired"),
                GoogleCalendarPage(
                    items=(),
                    next_page_token=None,
                    next_sync_token="token-full-2",
                ),
            ]
        )
        result = self.connector(transport).sync()

        self.assertTrue(result.full_resync_performed)
        self.assertEqual(result.session.status, ConnectorSessionStatus.COMPLETE)
        self.assertEqual(result.checkpoint, "token-full-2")
        self.assertEqual(result.health, ConnectorHealth.CURRENT)
        self.assertEqual(
            transport.calls,
            [("token-1", None), (None, None)],
        )
        task = self.repo.get_task("a", "task")
        self.assertEqual(task.obligation.lifecycle_status, LifecycleStatus.ACTIVE)
        bindings = self.recon.list_bindings("a")
        binding = next(b for b in bindings if b.external_entity_id == "e1")
        self.assertEqual(binding.state, BindingState.SOURCE_REMOVED)
        removals = [
            obs
            for obs in self.recon.list_observations("a")
            if obs.value_type.value == "SOURCE_REMOVED"
        ]
        self.assertEqual(len(removals), 1)
        sessions = self.connector_repo.list_sessions("a", "gcal")
        self.assertEqual(
            sum(s.error_code == "INVALID_SYNC_TOKEN" for s in sessions),
            1,
        )
        self.assertTrue(
            any(
                s.status is ConnectorSessionStatus.COMPLETE
                and s.cursor_after == "token-full-2"
                for s in sessions
            )
        )

    def test_explicit_cancelled_event_is_removal_evidence_not_local_delete(self):
        self.make_local_task()
        self.bind("e1")
        self.seed_complete_sync("e1")
        transport = ScriptedTransport(
            [
                GoogleCalendarPage(
                    items=(
                        event(
                            "e1",
                            "2026-09-20T12:00:00Z",
                            status="cancelled",
                        ),
                    ),
                    next_page_token=None,
                    next_sync_token="token-2",
                )
            ]
        )
        result = self.connector(transport).sync()
        self.assertEqual(result.session.deletion_count, 1)
        self.assertEqual(
            self.repo.get_task("a", "task").obligation.lifecycle_status,
            LifecycleStatus.ACTIVE,
        )

    def test_deleted_event_with_only_id_and_cancelled_status_is_supported(self):
        self.make_local_task()
        self.bind("e1")
        self.seed_complete_sync("e1")
        result = self.connector(
            ScriptedTransport(
                [
                    GoogleCalendarPage(
                        items=({"id": "e1", "status": "cancelled"},),
                        next_page_token=None,
                        next_sync_token="token-2",
                    )
                ]
            )
        ).sync()
        self.assertEqual(result.session.deletion_count, 1)
        row = self.repo.connection.execute(
            "SELECT source_revision,revision_order FROM source_records "
            "WHERE account_id=? AND external_entity_id=? "
            "ORDER BY rowid DESC LIMIT 1",
            ("a", "e1"),
        ).fetchone()
        self.assertIsNone(row["source_revision"])
        self.assertIsNone(row["revision_order"])
        self.assertEqual(
            self.repo.get_task("a", "task").obligation.lifecycle_status,
            LifecycleStatus.ACTIVE,
        )

    def test_concurrent_older_success_cannot_overwrite_newer_checkpoint(self):
        self.seed_complete_sync("e1")
        older = self.connector_repo.start_session(
            account_id="a",
            connector_id="gcal",
            is_full_sync=False,
            session_id="older-success",
        )
        newer = self.connector_repo.start_session(
            account_id="a",
            connector_id="gcal",
            is_full_sync=False,
            session_id="newer-success",
        )
        self.assertEqual(older.state_version_before, newer.state_version_before)

        completed = self.connector_repo.finish_complete(
            account_id="a",
            session_id=newer.id,
            checkpoint_after="token-2",
            page_count=1,
            record_count=0,
            deletion_count=0,
        )
        stale = self.connector_repo.finish_complete(
            account_id="a",
            session_id=older.id,
            checkpoint_after="stale-token",
            page_count=1,
            record_count=0,
            deletion_count=0,
        )

        state = self.connector_repo.get_state("a", "gcal")
        self.assertEqual(completed.status, ConnectorSessionStatus.COMPLETE)
        self.assertEqual(stale.status, ConnectorSessionStatus.FAILED)
        self.assertEqual(stale.error_code, "CONCURRENT_SYNC_CONFLICT")
        self.assertEqual(state.checkpoint, "token-2")
        self.assertEqual(state.health, ConnectorHealth.CURRENT)
        self.assertEqual(
            self.recon.current_source_availability("a", "gcal-source"),
            SourceAvailability.ACTIVE,
        )

    def test_concurrent_older_failure_cannot_regress_newer_health(self):
        self.seed_complete_sync("e1")
        older = self.connector_repo.start_session(
            account_id="a",
            connector_id="gcal",
            is_full_sync=False,
            session_id="older-failure",
        )
        newer = self.connector_repo.start_session(
            account_id="a",
            connector_id="gcal",
            is_full_sync=False,
            session_id="newer-complete",
        )
        self.connector_repo.finish_complete(
            account_id="a",
            session_id=newer.id,
            checkpoint_after="token-2",
            page_count=1,
            record_count=0,
            deletion_count=0,
        )
        failed = self.connector_repo.finish_failure(
            account_id="a",
            session_id=older.id,
            error_code="AUTH_UNAVAILABLE",
            page_count=0,
            record_count=0,
            deletion_count=0,
            unavailable=True,
        )

        state = self.connector_repo.get_state("a", "gcal")
        self.assertEqual(failed.status, ConnectorSessionStatus.FAILED)
        self.assertEqual(state.checkpoint, "token-2")
        self.assertEqual(state.health, ConnectorHealth.CURRENT)
        self.assertIsNone(state.latest_failure_reason)
        self.assertEqual(
            self.recon.current_source_availability("a", "gcal-source"),
            SourceAvailability.ACTIVE,
        )

    def test_concurrent_stale_410_cannot_clear_newer_checkpoint(self):
        self.seed_complete_sync("e1")
        older = self.connector_repo.start_session(
            account_id="a",
            connector_id="gcal",
            is_full_sync=False,
            session_id="older-410",
        )
        newer = self.connector_repo.start_session(
            account_id="a",
            connector_id="gcal",
            is_full_sync=False,
            session_id="newer-after-410",
        )
        self.connector_repo.finish_complete(
            account_id="a",
            session_id=newer.id,
            checkpoint_after="token-2",
            page_count=1,
            record_count=0,
            deletion_count=0,
        )
        failed, invalidated = self.connector_repo.finish_invalid_cursor(
            account_id="a",
            session_id=older.id,
            page_count=0,
            record_count=0,
            deletion_count=0,
        )

        state = self.connector_repo.get_state("a", "gcal")
        self.assertFalse(invalidated)
        self.assertEqual(failed.status, ConnectorSessionStatus.FAILED)
        self.assertEqual(failed.error_code, "CONCURRENT_SYNC_CONFLICT")
        self.assertEqual(state.checkpoint, "token-2")
        self.assertEqual(state.health, ConnectorHealth.CURRENT)
        self.assertEqual(
            self.recon.current_source_availability("a", "gcal-source"),
            SourceAvailability.ACTIVE,
        )

    def test_completed_session_is_terminal_and_repeat_finish_is_idempotent(self):
        self.seed_complete_sync("e1")
        session = self.connector_repo.start_session(
            account_id="a",
            connector_id="gcal",
            is_full_sync=False,
            session_id="terminal-session",
        )
        completed = self.connector_repo.finish_complete(
            account_id="a",
            session_id=session.id,
            checkpoint_after="token-2",
            page_count=1,
            record_count=0,
            deletion_count=0,
        )
        replayed_failure = self.connector_repo.finish_failure(
            account_id="a",
            session_id=session.id,
            error_code="AUTH_UNAVAILABLE",
            page_count=0,
            record_count=0,
            deletion_count=0,
            unavailable=True,
        )
        replayed_complete = self.connector_repo.finish_complete(
            account_id="a",
            session_id=session.id,
            checkpoint_after="stale-token",
            page_count=1,
            record_count=0,
            deletion_count=0,
        )

        state = self.connector_repo.get_state("a", "gcal")
        self.assertEqual(completed.status, ConnectorSessionStatus.COMPLETE)
        self.assertEqual(replayed_failure.status, ConnectorSessionStatus.COMPLETE)
        self.assertEqual(replayed_complete.status, ConnectorSessionStatus.COMPLETE)
        self.assertEqual(state.checkpoint, "token-2")
        self.assertEqual(state.health, ConnectorHealth.CURRENT)
        self.assertEqual(
            self.recon.current_source_availability("a", "gcal-source"),
            SourceAvailability.ACTIVE,
        )

    def test_auth_failure_marks_unavailable_without_advancing_checkpoint(self):
        self.seed_complete_sync("e1")
        from student_execution_os.connectors import GoogleCalendarAuthError

        result = self.connector(
            ScriptedTransport([GoogleCalendarAuthError("expired")])
        ).sync()
        self.assertEqual(result.session.status, ConnectorSessionStatus.FAILED)
        self.assertEqual(result.checkpoint, "token-1")
        self.assertEqual(result.health, ConnectorHealth.UNAVAILABLE)
        self.assertEqual(
            self.recon.current_source_availability("a", "gcal-source"),
            SourceAvailability.UNAVAILABLE,
        )


if __name__ == "__main__":
    unittest.main()