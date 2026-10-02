"""End-to-end Assistant acceptance scenarios from the remediation spec (§21, §29, §54).

The fixture provider stands in for the model and only emits *typed semantic intent*
(client refs, temporal transforms, relative anchors); every timestamp asserted here is
computed by the server. These tests prove the contract, not live model quality.
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from student_execution_os.agent import AuthenticatedPrincipal, SQLiteAssistantService
from student_execution_os.domain.clock import FrozenClock
from student_execution_os.domain.errors import ValidationError
from student_execution_os.domain.model import (
    ActorCategory,
    AttendancePolicy,
    EventTimeSemantics,
    Importance,
    ObligationCategory,
)
from student_execution_os.persistence import SQLiteCanonicalRepository


ZONE = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=ZONE)
GUARDED = {
    "kind": "SHIFT_WITH_GUARD_AND_FALLBACK",
    "delta_minutes": 180,
    "guard": {"not_after_local_time": "22:00"},
    "fallback": {"relative_day": "NEXT_MORNING", "preferred_local_time": "10:00", "precision": "APPROXIMATE"},
}


def _action(command, payload, version=None, **extra):
    return {"command": command, "payload": payload, "confidence": 0.95, "unresolved_fields": [],
            "expected_version": version, "requires_confirmation": False, **extra}


class ConversationProvider:
    """Answers each turn of the §54 conversation from context only (no stored state)."""

    name = "acceptance-fixture"
    model = "fixture"

    def interpret(self, text, context):
        meeting = next(item for item in context["obligations"] if item["title"] == "Встреча с Ариадной")
        if text.startswith("Перенеси"):
            return {"message": "proposal", "actions": [_action(
                "RESCHEDULE", {"obligation_id": meeting["id"], "temporal_transform": GUARDED}, meeting["version"],
                field_provenance={"obligation_id": "MODEL_EXPLICIT", "temporal_transform": "MODEL_EXPLICIT"},
            )]}
        if text.startswith("Лучше в 10:30"):
            previous = context["assistant_session"]["previous_actions"][0]
            when = datetime.fromisoformat(previous["payload"]["when"]).astimezone(ZONE).replace(hour=10, minute=30)
            return {"message": "corrected", "actions": [_action(
                "RESCHEDULE", {"obligation_id": meeting["id"], "when": when.isoformat()}, meeting["version"],
                field_provenance={"obligation_id": "MODEL_EXPLICIT", "when": "MODEL_EXPLICIT"},
            )]}
        if text.startswith("И напомни"):
            return {"message": "reminder", "actions": [_action(
                "UPDATE_EVENT", {"obligation_id": meeting["id"], "remind_before_minutes": 30}, meeting["version"],
            )]}
        if text.startswith("Отмени последнее"):
            return {"message": "undo", "actions": [_action("UNDO_LAST", {})]}
        raise AssertionError(text)


class PlanProvider:
    """«Перенеси встречу на шесть, напомни о ней за двадцать минут, а после неё поставь час на отчёт.»"""

    name = "plan-fixture"
    model = "fixture"

    def __init__(self, *, transform=None, ambiguous=False, task=False, bad_reference=False):
        self.transform, self.ambiguous, self.task, self.bad_reference = transform, ambiguous, task, bad_reference

    def interpret(self, text, context):
        meeting = next(item for item in context["obligations"] if item["title"] == "Встреча с Ариадной")
        if self.ambiguous:
            move = _action("RESCHEDULE", {"target_text": "встречу", "when": "2026-10-01T18:00:00+03:00"},
                           unresolved_fields=["target"])
        else:
            payload = {"obligation_id": meeting["id"]}
            payload.update({"temporal_transform": self.transform} if self.transform
                           else {"when": "2026-10-01T18:00:00+03:00"})
            move = _action("RESCHEDULE", payload, meeting["version"])
        move["client_ref"] = "move"
        anchor = {"action": "elsewhere" if self.bad_reference else "move", "anchor": "END", "offset_minutes": 0}
        if self.task:
            work = _action("CREATE_TASK", {"title": "Отчёт", "estimated_total_effort_minutes": 60,
                                           "actual_cutoff": {"state": "ABSENT"}, "relative_to": anchor},
                           client_ref="report", depends_on=["move"])
        else:
            work = _action("CREATE_EVENT", {"title": "Отчёт", "duration_minutes": 60, "relative_to": anchor},
                           client_ref="report", depends_on=["move"])
        actions = [move, work]
        if not self.ambiguous:
            remind = _action("UPDATE_EVENT", {"obligation_id": meeting["id"], "remind_before_minutes": 20},
                             meeting["version"], client_ref="remind", depends_on=["move"])
            actions.insert(1, remind)
        return {"message": "plan", "actions": actions}


class AcceptanceBase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.repository = SQLiteCanonicalRepository(
            str(Path(self.temporary.name) / "acceptance.sqlite"), clock=FrozenClock(NOW),
        )
        self.repository.initialize()
        self.repository.create_account("account")
        self.repository.create_event(
            account_id="account", obligation_id="ariadne", title="Встреча с Ариадной",
            description="обсудить курсовую", category=ObligationCategory.MEETING, importance=Importance.HIGH,
            time_semantics=EventTimeSemantics.FIXED_INTERVAL,
            starts_at=NOW.replace(hour=20), ends_at=NOW.replace(hour=21),
            attendance_policy=AttendancePolicy.REQUIRED, actor=ActorCategory.USER_UI,
        )
        self.principal = AuthenticatedPrincipal("account", "user", "client")

    def tearDown(self):
        self.repository.close()
        self.temporary.cleanup()

    def service(self, provider):
        return SQLiteAssistantService(self.repository, self.principal, provider=provider)

    def meeting(self):
        return self.repository.get_event("account", "ariadne")

    def local(self, value):
        return value.astimezone(ZONE).strftime("%Y-%m-%d %H:%M")

    def lead(self):
        row = self.repository.connection.execute(
            "SELECT lead_minutes FROM event_reminders WHERE account_id='account' AND event_id='ariadne'"
        ).fetchone()
        return None if row is None else row[0]

    def created(self, title):
        return self.repository.connection.execute(
            "SELECT o.id,o.kind,e.starts_at,e.ends_at,t.actionable_from FROM obligations o "
            "LEFT JOIN events e ON e.obligation_id=o.id LEFT JOIN tasks t ON t.obligation_id=o.id "
            "WHERE o.account_id='account' AND o.title=?", (title,),
        ).fetchone()


class ConversationAcceptanceTest(AcceptanceBase):
    def test_guarded_reschedule_correction_reminder_and_undo_last(self):
        service = self.service(ConversationProvider())
        context = {"timezone": "Europe/Moscow"}

        first = service.interpret("Перенеси встречу с Ариадной на три часа позже. "
                                  "Но если позже 22:00, то на утро, часов на 10 примерно.", context)
        proposal = first["actions"][0]
        self.assertEqual(self.local(datetime.fromisoformat(proposal["payload"]["when"])), "2026-10-02 10:00")
        self.assertEqual(proposal["resolution"]["reason"], "GUARD_TRIGGERED_FALLBACK")
        self.assertEqual(proposal["resolution"]["precision"], "APPROXIMATE")  # not fabricated exact evidence
        self.assertFalse(first["mutated_canonical_state"])
        self.assertEqual(self.local(self.meeting().interval.starts_at), "2026-10-01 20:00")

        second = service.interpret("Лучше в 10:30.", {**context, "previous_batch_id": first["batch_id"]})
        corrected = second["actions"][0]
        self.assertEqual(self.local(datetime.fromisoformat(corrected["payload"]["when"])), "2026-10-02 10:30")
        self.assertEqual(corrected["resolution"]["precision"], "EXACT")
        service.apply({"batch_id": second["batch_id"], "action_ids": [corrected["id"]], "idempotency_key": "move"})
        meeting = self.meeting()
        self.assertEqual(self.local(meeting.interval.starts_at), "2026-10-02 10:30")
        self.assertEqual(meeting.interval.ends_at - meeting.interval.starts_at, timedelta(hours=1))
        self.assertEqual((meeting.obligation.description, meeting.obligation.importance),
                         ("обсудить курсовую", Importance.HIGH))  # unrelated fields unchanged

        third = service.interpret("И напомни за полчаса", {**context, "previous_batch_id": second["batch_id"]})
        service.apply({"batch_id": third["batch_id"], "action_ids": [third["actions"][0]["id"]],
                       "idempotency_key": "remind"})
        self.assertEqual(self.lead(), 30)

        fourth = service.interpret("Отмени последнее", {**context, "previous_batch_id": third["batch_id"]})
        undo = service.apply({"batch_id": fourth["batch_id"], "action_ids": [fourth["actions"][0]["id"]],
                              "idempotency_key": "undo"})
        self.assertEqual(undo["results"][0]["undid_action_id"], third["actions"][0]["id"])
        self.assertIsNone(self.lead())  # the reminder change is undone…
        self.assertEqual(self.local(self.meeting().interval.starts_at), "2026-10-02 10:30")  # …the move stays

    def test_preview_time_edit_overrides_the_approximate_transform(self):
        service = self.service(ConversationProvider())
        preview = service.interpret("Перенеси встречу с Ариадной на три часа позже, но не позже 22, иначе утром",
                                    {"timezone": "Europe/Moscow"})
        action = preview["actions"][0]
        service.apply({"batch_id": preview["batch_id"], "action_ids": [action["id"]], "idempotency_key": "edit",
                       "edits": {action["id"]: {"when": "2026-10-02T10:30:00+03:00"}}})
        self.assertEqual(self.local(self.meeting().interval.starts_at), "2026-10-02 10:30")


class SourceOwnedEventTest(AcceptanceBase):
    def setUp(self):
        super().setUp()
        self.repository.connection.execute(
            "INSERT INTO external_identities(account_id,source_system_id,external_uid,local_kind,local_id,"
            "first_seen_at,last_seen_at) VALUES ('account','ical:hse','uid-ariadne','EVENT','ariadne',?,?)",
            (NOW.isoformat(), NOW.isoformat()),
        )
        self.repository.connection.commit()

    def test_imported_event_move_is_blocked_in_preview_and_refused_at_apply(self):
        service = self.service(ConversationProvider())
        preview = service.interpret("Перенеси встречу с Ариадной на три часа позже, иначе утром",
                                    {"timezone": "Europe/Moscow"})
        action = preview["actions"][0]
        self.assertEqual(action["blocked"]["code"], "IMPORTED_EVENT_SOURCE_OWNED")
        with self.assertRaises(ValidationError):
            service.apply({"batch_id": preview["batch_id"], "action_ids": [action["id"]], "idempotency_key": "move"})
        self.assertEqual(self.local(self.meeting().interval.starts_at), "2026-10-01 20:00")

    def test_a_personal_reminder_on_an_imported_event_is_allowed(self):
        service = self.service(ConversationProvider())
        preview = service.interpret("И напомни за полчаса")
        self.assertNotIn("blocked", preview["actions"][0])
        service.apply({"batch_id": preview["batch_id"], "action_ids": [preview["actions"][0]["id"]],
                       "idempotency_key": "remind"})
        self.assertEqual(self.lead(), 30)


class MultiActionAcceptanceTest(AcceptanceBase):
    def test_dependent_time_is_derived_from_the_earlier_action_and_follows_its_correction(self):
        service = self.service(PlanProvider())
        preview = service.interpret("Перенеси встречу на шесть, напомни о ней за двадцать минут, "
                                    "а после неё поставь час на отчёт.")
        move, remind, report = preview["actions"]
        self.assertEqual((remind["depends_on"], report["depends_on"]), ([move["id"]], [move["id"]]))
        self.assertEqual(self.local(datetime.fromisoformat(report["payload"]["starts_at"])), "2026-10-01 19:00")
        self.assertEqual(self.local(datetime.fromisoformat(report["payload"]["ends_at"])), "2026-10-01 20:00")
        self.assertEqual(report["resolution"]["reason"], "RELATIVE_TO_ACTION")
        self.assertEqual(report["provenance"]["fields"]["starts_at"], "DERIVED")

        # The user moves the meeting to 18:30 in the preview; the report follows it.
        service.apply({"batch_id": preview["batch_id"], "action_ids": [move["id"], remind["id"], report["id"]],
                       "idempotency_key": "plan", "edits": {move["id"]: {"when": "2026-10-01T18:30:00+03:00"}}})
        self.assertEqual(self.local(self.meeting().interval.starts_at), "2026-10-01 18:30")
        self.assertEqual(self.lead(), 20)
        row = self.created("Отчёт")
        self.assertEqual(row["kind"], "EVENT")
        self.assertEqual(self.local(datetime.fromisoformat(row["starts_at"])), "2026-10-01 19:30")
        self.assertEqual(self.local(datetime.fromisoformat(row["ends_at"])), "2026-10-01 20:30")

    def test_explicit_edit_of_the_dependent_time_wins(self):
        service = self.service(PlanProvider())
        preview = service.interpret("план")
        move, remind, report = preview["actions"]
        service.apply({"batch_id": preview["batch_id"], "action_ids": [move["id"], remind["id"], report["id"]],
                       "idempotency_key": "plan", "edits": {
                           move["id"]: {"when": "2026-10-01T18:30:00+03:00"},
                           report["id"]: {"starts_at": "2026-10-01T21:00:00+03:00",
                                          "ends_at": "2026-10-01T22:00:00+03:00"},
                       }})
        self.assertEqual(self.local(datetime.fromisoformat(self.created("Отчёт")["starts_at"])), "2026-10-01 21:00")

    def test_work_task_after_an_approximate_fallback_is_approximate_too(self):
        service = self.service(PlanProvider(transform=GUARDED, task=True))
        preview = service.interpret("план", {"timezone": "Europe/Moscow"})
        move, _remind, report = preview["actions"]
        self.assertEqual(report["resolution"]["precision"], "APPROXIMATE")
        self.assertEqual(self.local(datetime.fromisoformat(report["payload"]["actionable_from"])), "2026-10-02 11:00")
        service.apply({"batch_id": preview["batch_id"], "action_ids": [a["id"] for a in preview["actions"]],
                       "idempotency_key": "plan"})
        self.assertEqual(self.local(datetime.fromisoformat(self.created("Отчёт")["actionable_from"])), "2026-10-02 11:00")

    def test_ambiguous_prerequisite_leaves_the_dependent_time_unresolved_until_picked(self):
        service = self.service(PlanProvider(ambiguous=True))
        preview = service.interpret("план")
        move, report = preview["actions"]
        self.assertIn("starts_at", report["unresolved_fields"])
        self.assertNotIn("starts_at", report["payload"])
        with self.assertRaises(ValidationError):
            service.apply({"batch_id": preview["batch_id"], "action_ids": [move["id"], report["id"]],
                           "idempotency_key": "too-early"})
        self.assertIsNone(self.created("Отчёт"))
        service.apply({"batch_id": preview["batch_id"], "action_ids": [move["id"], report["id"]],
                       "idempotency_key": "picked",
                       "edits": {move["id"]: {"obligation_id": "ariadne", "expected_version": self.meeting().obligation.version}}})
        self.assertEqual(self.local(datetime.fromisoformat(self.created("Отчёт")["starts_at"])), "2026-10-01 19:00")

    def apply_plan(self, service):
        preview = service.interpret("план")
        service.apply({"batch_id": preview["batch_id"], "action_ids": [a["id"] for a in preview["actions"]],
                       "idempotency_key": "plan"})
        return preview

    def test_undo_button_reverts_the_whole_plan_including_the_created_item(self):
        service = self.service(PlanProvider())
        self.apply_plan(service)
        self.assertIsNotNone(self.created("Отчёт"))
        first = service.undo({"idempotency_key": "undo-1"})
        steps = first["results"][0]["undone"]
        self.assertEqual([step["operation"] for step in steps], ["event.delete", "event.update", "event.update"])
        self.assertIsNone(self.created("Отчёт"))
        self.assertEqual(self.local(self.meeting().interval.starts_at), "2026-10-01 20:00")
        self.assertIsNone(self.lead())
        # A retried request (lost response) replays; it does not undo something else.
        self.assertTrue(service.undo({"idempotency_key": "undo-1"})["replayed"])
        with self.assertRaises(ValidationError):
            service.undo({"idempotency_key": "undo-2"})  # nothing reversible is left

    def test_undo_refuses_to_overwrite_a_later_change_and_reverts_nothing(self):
        from student_execution_os.domain.errors import VersionConflict
        from student_execution_os.sync.commands import Commands

        service = self.service(PlanProvider())
        self.apply_plan(service)
        report = self.created("Отчёт")
        Commands(self.repository, account_id="account", actor=ActorCategory.USER_UI, now=NOW).run(
            "event.update", report["id"], {"title": "Отчёт по физике"})
        with self.assertRaises(VersionConflict):
            service.undo({"idempotency_key": "undo"})
        self.assertEqual(self.local(self.meeting().interval.starts_at), "2026-10-01 18:00")  # rolled back as a unit
        self.assertEqual(self.lead(), 20)

    def test_a_stale_undo_button_cannot_undo_a_newer_change(self):
        from student_execution_os.domain.errors import VersionConflict

        service = self.service(PlanProvider())
        self.apply_plan(service)  # apply key "plan"
        reminder = self.service(ConversationProvider())
        later = reminder.interpret("И напомни за полчаса")
        reminder.apply({"batch_id": later["batch_id"], "action_ids": [later["actions"][0]["id"]],
                        "idempotency_key": "later"})
        with self.assertRaises(VersionConflict):
            service.undo({"idempotency_key": "old-toast", "apply_idempotency_key": "plan"})
        self.assertEqual(self.lead(), 30)  # the newer change is untouched
        service.undo({"idempotency_key": "new-toast", "apply_idempotency_key": "later"})
        self.assertEqual(self.lead(), 20)

    def test_reference_outside_depends_on_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.service(PlanProvider(bad_reference=True)).interpret("план")


if __name__ == "__main__":
    unittest.main()
