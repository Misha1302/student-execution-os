from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from student_execution_os.agent import AuthenticatedPrincipal, SQLiteAssistantService
from student_execution_os.domain.clock import FrozenClock
from student_execution_os.domain.errors import ValidationError
from student_execution_os.persistence import SQLiteCanonicalRepository
from student_execution_os.planning import SQLitePlanningStateSource


NOW = datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc)


class PlannerControlProvider:
    name = "planner-control-fixture"
    model = "fixture"

    def interpret(self, text, context):
        return {"message": "protected morning", "actions": [{
            "command": "CREATE_TIME_CONSTRAINT",
            "payload": {
                "type": "UNAVAILABLE",
                "starts_at": "2026-10-03T00:00:00+00:00",
                "ends_at": "2026-10-03T09:00:00+00:00",
                "reason": "USER_REQUESTED_NO_WORK_BEFORE_NOON",
            },
            "confidence": 0.97,
            "unresolved_fields": [],
            "expected_version": None,
            "requires_confirmation": False,
            "field_provenance": {
                "type": "MODEL_EXPLICIT",
                "starts_at": "MODEL_EXPLICIT",
                "ends_at": "MODEL_EXPLICIT",
                "reason": "MODEL_INFERRED",
            },
        }]}


class AssistantPlannerControlTest(unittest.TestCase):
    def test_natural_language_control_creates_canonical_constraint_not_plan_block(self):
        with tempfile.TemporaryDirectory() as directory:
            with SQLiteCanonicalRepository(
                str(Path(directory) / "planner-control.sqlite"), clock=FrozenClock(NOW),
            ) as repository:
                repository.initialize()
                repository.create_account("account")
                service = SQLiteAssistantService(
                    repository, AuthenticatedPrincipal("account", "user", "client"),
                    provider=PlannerControlProvider(),
                )
                preview = service.interpret("Завтра ничего до 12", {"timezone": "Europe/Moscow"})
                action = preview["actions"][0]
                self.assertEqual(action["command"], "CREATE_TIME_CONSTRAINT")
                self.assertEqual(
                    repository.connection.execute("SELECT count(*) FROM user_time_constraints").fetchone()[0],
                    0,
                )
                result = service.apply({
                    "batch_id": preview["batch_id"],
                    "action_ids": [action["id"]],
                    "idempotency_key": "planner-control",
                })
                self.assertFalse(result["replayed"])
                constraints = SQLitePlanningStateSource(repository).list_time_constraints("account")
                self.assertEqual(len(constraints), 1)
                self.assertEqual(constraints[0].type.value, "UNAVAILABLE")
                self.assertEqual(constraints[0].reason, "USER_REQUESTED_NO_WORK_BEFORE_NOON")
                self.assertEqual(repository.connection.execute("SELECT count(*) FROM plan_blocks").fetchone()[0], 0)


# The five soft-preference phrases → the typed preference a provider is expected to emit.
PREFERENCE_PHRASES = {
    "Оставь вечером хотя бы час свободным.": {
        "kind": "KEEP_FREE", "window_start": "18:00", "window_end": "23:00", "minutes": 60,
        "date_from": "2026-10-02", "date_until": "2026-10-02"},
    "Завтра сделай день полегче.": {
        "kind": "WORK_LIMIT", "date_from": "2026-10-03", "date_until": "2026-10-03"},
    "Не ставь сложное сразу после подъёма.": {
        "kind": "AVOID_WORK", "anchor": "WAKE", "target": "DEMANDING", "minutes": 90,
        "date_from": "2026-10-02", "date_until": None},
    "После пары дай мне полчаса передохнуть.": {
        "kind": "REST_AFTER_EVENTS", "target": "CLASSES", "minutes": 30,
        "date_from": "2026-10-02", "date_until": None},
    "Учёбу желательно закончить до девяти.": {
        "kind": "AVOID_WORK", "target": "STUDY", "window_start": "21:00",
        "date_from": "2026-10-02", "date_until": None},
}


class PreferenceProvider:
    name = "planner-preference-fixture"
    model = "fixture"

    def __init__(self, payload):
        self.payload = payload

    def interpret(self, text, context):
        return {"message": "preference", "actions": [{
            "command": "CREATE_PLANNING_PREFERENCE",
            "payload": dict(self.payload),
            "confidence": 0.9,
            "unresolved_fields": [],
            "expected_version": None,
            "requires_confirmation": False,
            "field_provenance": {key: "MODEL_EXPLICIT" for key in self.payload},
        }]}


class AssistantPlanningPreferenceTest(unittest.TestCase):
    def service(self, repository, payload):
        return SQLiteAssistantService(
            repository, AuthenticatedPrincipal("account", "user", "client"), provider=PreferenceProvider(payload),
        )

    def test_each_soft_phrase_becomes_one_canonical_preference_and_undo_removes_it(self):
        from student_execution_os.planning.preference_store import SQLitePlanningPreferenceRepository
        from student_execution_os.planning.preferences import LIGHT_DAY_WORK_MINUTES

        for phrase, payload in PREFERENCE_PHRASES.items():
            with self.subTest(phrase=phrase), tempfile.TemporaryDirectory() as directory:
                with SQLiteCanonicalRepository(str(Path(directory) / "p.sqlite"), clock=FrozenClock(NOW)) as repository:
                    repository.initialize()
                    repository.create_account("account")
                    service = self.service(repository, payload)
                    preview = service.interpret(phrase, {"timezone": "UTC"})
                    action = preview["actions"][0]
                    self.assertEqual(action["command"], "CREATE_PLANNING_PREFERENCE")
                    store = SQLitePlanningPreferenceRepository(repository)
                    self.assertEqual(store.list("account"), [])  # preview never writes
                    service.apply({"batch_id": preview["batch_id"], "action_ids": [action["id"]],
                                   "idempotency_key": "pref-apply"})
                    stored = store.list("account")
                    self.assertEqual(len(stored), 1)
                    self.assertEqual(stored[0].kind.value, payload["kind"])
                    if payload["kind"] == "WORK_LIMIT":
                        self.assertEqual(stored[0].minutes, LIGHT_DAY_WORK_MINUTES)
                    self.assertEqual(repository.connection.execute("SELECT count(*) FROM plan_blocks").fetchone()[0], 0)
                    service.undo({"apply_idempotency_key": "pref-apply", "idempotency_key": "pref-undo"})
                    self.assertEqual(store.list("account"), [])

    def test_invalid_or_opaque_preferences_are_rejected_before_preview(self):
        bad = [
            {"kind": "KEEP_FREE", "minutes": 60, "date_from": "2026-10-02"},                      # no window
            {"kind": "AVOID_WORK", "target": "SOMETIMES", "window_start": "21:00", "date_from": "2026-10-02"},
            {"kind": "AVOID_WORK", "window_start": "21:00", "date_from": "2026-10-02", "planner_prompt": "be nice"},
            {"kind": "MAKE_IT_NICE", "date_from": "2026-10-02"},
        ]
        for payload in bad:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as directory:
                with SQLiteCanonicalRepository(str(Path(directory) / "p.sqlite"), clock=FrozenClock(NOW)) as repository:
                    repository.initialize()
                    repository.create_account("account")
                    with self.assertRaises(ValidationError):
                        self.service(repository, payload).interpret("сделай красиво", {"timezone": "UTC"})
                    self.assertEqual(
                        repository.connection.execute("SELECT count(*) FROM assistant_batches").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
