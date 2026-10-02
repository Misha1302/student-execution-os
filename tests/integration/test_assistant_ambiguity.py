"""Server-side target ambiguity: the model proposes a pick, the server decides if it is unique."""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from student_execution_os.agent import AuthenticatedPrincipal, SQLiteAssistantService
from student_execution_os.domain.clock import FrozenClock
from student_execution_os.domain.errors import ValidationError
from student_execution_os.domain.model import ActorCategory, HardCutoff, Importance, ObligationCategory
from student_execution_os.persistence import SQLiteCanonicalRepository
from student_execution_os.sync.commands import Commands

ZONE = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 10, 5, 9, 0, tzinfo=ZONE)  # Monday


class PickProvider:
    """Proposes RESCHEDULE of whatever `pick(context)` returns — the model's choice."""

    name = "pick-fixture"
    model = "fixture"

    def __init__(self, pick, when, target_text=None):
        self.pick, self.when, self.target_text = pick, when, target_text

    def interpret(self, text, context):
        item = self.pick(context)
        key = "reminder_id" if item["kind"] == "REMINDER" else "obligation_id"
        payload = {key: item["id"], "when": self.when.isoformat()}
        if self.target_text:
            payload["target_text"] = self.target_text
        return {"message": "moved", "actions": [{
            "command": "RESCHEDULE", "payload": payload, "confidence": 0.99, "unresolved_fields": [],
            "expected_version": item.get("version"), "requires_confirmation": False,
            "field_provenance": {key: "MODEL_EXPLICIT", "when": "MODEL_EXPLICIT"},
        }]}


def by_title(title, nth=0, kind=None):
    def pick(context):
        items = [i for i in [*context["obligations"], *context["reminders"]]
                 if i["title"] == title and (kind is None or i["kind"] == kind)]
        items.sort(key=lambda i: i.get("starts_at") or i.get("remind_at") or i.get("due") or "")
        return items[nth]
    return pick


class AssistantAmbiguityTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.repository = SQLiteCanonicalRepository(str(Path(self.directory.name) / "a.sqlite"), clock=FrozenClock(NOW))
        self.repository.initialize()
        self.repository.create_account("account")
        self.repository.create_account("other")

    def tearDown(self):
        self.repository.close()
        self.directory.cleanup()

    def event(self, eid, title, starts, account="account", minutes=60):
        self.repository.create_fixed_event(account_id=account, obligation_id=eid, title=title, starts_at=starts,
                                           ends_at=starts + timedelta(minutes=minutes), actor=ActorCategory.SYSTEM)

    def service(self, provider):
        return SQLiteAssistantService(self.repository, AuthenticatedPrincipal("account", "user", "client"),
                                      provider=provider)

    def interpret(self, text, provider, **context):
        return self.service(provider).interpret(text, {"timezone": "Europe/Moscow", **context})

    def assert_ambiguous(self, preview, expected_ids):
        action = preview["actions"][0]
        self.assertIn("target", action["unresolved_fields"])
        self.assertNotIn("obligation_id", action["payload"])
        self.assertNotIn("reminder_id", action["payload"])
        self.assertEqual(sorted(c["id"] for c in action["target_candidates"]), sorted(expected_ids))
        return action

    def assert_continues(self, preview, entity_id):
        action = preview["actions"][0]
        self.assertNotIn("target", action["unresolved_fields"])
        self.assertEqual(action["payload"].get("obligation_id") or action["payload"].get("reminder_id"), entity_id)
        self.assertNotIn("target_candidates", action)

    # -- identical / similar titles --------------------------------------------------------
    def test_identical_titles_need_the_user_to_pick_and_apply_waits_for_it(self):
        self.event("ariadna-mon", "Встреча с Ариадной", NOW.replace(hour=15))
        self.event("ariadna-fri", "Встреча с Ариадной", NOW.replace(hour=15) + timedelta(days=4))
        when = NOW.replace(hour=18)
        preview = self.interpret("Перенеси встречу с Ариадной на 18:00", PickProvider(by_title("Встреча с Ариадной"), when))
        action = self.assert_ambiguous(preview, ["ariadna-mon", "ariadna-fri"])
        self.assertEqual(action["target_guard"], "MULTIPLE_PLAUSIBLE")
        service = self.service(PickProvider(by_title("Встреча с Ариадной"), when))
        with self.assertRaises(ValidationError):
            service.apply({"batch_id": preview["batch_id"], "action_ids": [action["id"]], "idempotency_key": "k1"})
        self.assertEqual(self.repository.get_event("account", "ariadna-mon").interval.starts_at, NOW.replace(hour=15))
        service.apply({"batch_id": preview["batch_id"], "action_ids": [action["id"]], "idempotency_key": "k2",
                       "edits": {action["id"]: {"obligation_id": "ariadna-fri", "expected_version": 1}}})
        self.assertEqual(self.repository.get_event("account", "ariadna-fri").interval.starts_at, when)
        self.assertEqual(self.repository.get_event("account", "ariadna-mon").interval.starts_at, NOW.replace(hour=15))

    def test_the_more_specific_title_wins_and_a_contradicting_pick_is_not_trusted(self):
        self.event("plain", "Встреча с Ариадной", NOW.replace(hour=15))
        self.event("thesis", "Встреча с Ариадной по курсовой", NOW.replace(hour=16))
        when = NOW.replace(hour=19)
        preview = self.interpret("Перенеси встречу с Ариадной по курсовой на 19:00",
                                 PickProvider(by_title("Встреча с Ариадной по курсовой"), when))
        self.assert_continues(preview, "thesis")
        preview = self.interpret("Перенеси встречу с Ариадной на 19:00",
                                 PickProvider(by_title("Встреча с Ариадной"), when))
        self.assert_continues(preview, "plain")
        wrong = self.interpret("Перенеси встречу с Ариадной по курсовой на 19:00",
                               PickProvider(by_title("Встреча с Ариадной"), when))
        action = self.assert_ambiguous(wrong, ["thesis", "plain"])
        self.assertEqual(action["target_guard"], "PICK_CONTRADICTS_TEXT")

    def test_unrelated_titles_do_not_create_ambiguity(self):
        self.event("ariadna", "Встреча с Ариадной", NOW.replace(hour=15))
        self.event("boris", "Встреча с Борисом", NOW.replace(hour=16))
        preview = self.interpret("Перенеси встречу с Ариадной на 18:00",
                                 PickProvider(by_title("Встреча с Ариадной"), NOW.replace(hour=18)))
        self.assert_continues(preview, "ariadna")

    # -- dates and times -------------------------------------------------------------------
    def test_same_person_on_different_dates_is_disambiguated_by_an_explicit_date(self):
        self.event("ariadna-wed", "Встреча с Ариадной", NOW.replace(hour=15) + timedelta(days=2))
        self.event("ariadna-fri", "Встреча с Ариадной", NOW.replace(hour=15) + timedelta(days=4))
        when = NOW.replace(hour=12) + timedelta(days=5)  # Saturday — the destination is not evidence
        right = self.interpret("Встречу с Ариадной в среду перенеси на субботу 12:00",
                               PickProvider(by_title("Встреча с Ариадной", 0), when))
        self.assert_continues(right, "ariadna-wed")
        wrong = self.interpret("Встречу с Ариадной в среду перенеси на субботу 12:00",
                               PickProvider(by_title("Встреча с Ариадной", 1), when))
        self.assert_ambiguous(wrong, ["ariadna-wed", "ariadna-fri"])
        destination_only = self.interpret("Перенеси встречу с Ариадной на пятницу 18:00",
                                          PickProvider(by_title("Встреча с Ариадной", 1),
                                                       NOW.replace(hour=18) + timedelta(days=4)))
        self.assert_ambiguous(destination_only, ["ariadna-wed", "ariadna-fri"])

    def test_explicit_time_disambiguates_same_day_items(self):
        self.event("call-10", "Созвон", NOW.replace(hour=10))
        self.event("call-16", "Созвон", NOW.replace(hour=16))
        preview = self.interpret("Созвон в 16 перенеси на 17:00",
                                 PickProvider(by_title("Созвон", 1), NOW.replace(hour=17)))
        self.assert_continues(preview, "call-16")

    # -- kinds -----------------------------------------------------------------------------
    def test_different_kinds_with_one_title_are_ambiguous_unless_the_kind_is_named(self):
        task = self.repository.create_task(
            account_id="account", obligation_id="essay-task", title="Эссе", category=ObligationCategory.HOMEWORK,
            importance=Importance.NORMAL, estimated_total_effort_minutes=60, remaining_effort_minutes=60,
            splittable=True, min_chunk_minutes=None, max_chunk_minutes=None, actionable_from=None, target_at=None,
            actual_cutoff=HardCutoff.known(NOW + timedelta(days=2)), actor=ActorCategory.SYSTEM,
        )
        self.assertEqual(task.obligation.id, "essay-task")
        Commands(self.repository, account_id="account", actor=ActorCategory.USER_UI, now=NOW).run(
            "reminder.create", "reminder-essay-1", {"title": "Эссе", "remind_at": (NOW + timedelta(hours=3)).isoformat()})
        when = NOW + timedelta(days=1)
        plain = self.interpret("Перенеси эссе на завтра", PickProvider(by_title("Эссе", kind="REMINDER"), when))
        self.assert_ambiguous(plain, ["essay-task", "reminder-essay-1"])
        named = self.interpret("Перенеси напоминание про эссе на завтра",
                               PickProvider(by_title("Эссе", kind="REMINDER"), when))
        self.assert_continues(named, "reminder-essay-1")

    # -- conversation ----------------------------------------------------------------------
    def test_pronoun_and_repeated_title_continue_the_previous_turn(self):
        self.event("ariadna-mon", "Встреча с Ариадной", NOW.replace(hour=15))
        self.event("ariadna-fri", "Встреча с Ариадной", NOW.replace(hour=15) + timedelta(days=4))
        # "в пятницу" is the source here; the destination (Saturday) is never evidence.
        first = self.interpret("Встречу с Ариадной в пятницу перенеси на субботу 17:00",
                               PickProvider(by_title("Встреча с Ариадной", 1), NOW.replace(hour=17) + timedelta(days=5)))
        self.assert_continues(first, "ariadna-fri")
        pronoun = self.interpret("Нет, лучше её на 18:00",
                                 PickProvider(by_title("Встреча с Ариадной", 1), NOW.replace(hour=18) + timedelta(days=5)),
                                 previous_batch_id=first["batch_id"])
        self.assert_continues(pronoun, "ariadna-fri")
        repeated = self.interpret("Перенеси встречу с Ариадной ещё на час позже",
                                  PickProvider(by_title("Встреча с Ариадной", 1), NOW.replace(hour=19) + timedelta(days=5)),
                                  previous_batch_id=first["batch_id"])
        self.assert_continues(repeated, "ariadna-fri")

    # -- authorization ---------------------------------------------------------------------
    def test_items_outside_the_authorized_context_are_rejected(self):
        self.event("mine", "Встреча с Ариадной", NOW.replace(hour=15))
        self.event("theirs", "Встреча с Ариадной", NOW.replace(hour=15), account="other")
        foreign = {"id": "theirs", "kind": "EVENT", "version": 1}
        with self.assertRaises(ValidationError):
            self.interpret("Перенеси встречу с Ариадной на 18:00",
                           PickProvider(lambda _context: foreign, NOW.replace(hour=18)))
        invented = {"id": "event-that-never-existed", "kind": "EVENT", "version": 1}
        with self.assertRaises(ValidationError):
            self.interpret("Перенеси встречу с Ариадной на 18:00",
                           PickProvider(lambda _context: invented, NOW.replace(hour=18)))
        self.event("old", "Старая встреча", NOW - timedelta(days=40))
        self.repository.cancel_obligation(account_id="account", obligation_id="old", expected_version=1,
                                          actor=ActorCategory.USER_UI)
        hidden = {"id": "old", "kind": "EVENT", "version": 2}  # exists, but was never shown to the model
        with self.assertRaises(ValidationError):
            self.interpret("Перенеси старую встречу на 18:00", PickProvider(lambda _context: hidden, NOW.replace(hour=18)))
        self.assertEqual(self.repository.get_event("other", "theirs").interval.starts_at, NOW.replace(hour=15))


if __name__ == "__main__":
    unittest.main()
