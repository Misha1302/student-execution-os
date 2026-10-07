"""Assistant edits of a Task's checklist (CHECKLIST_STEP).

The Assistant proposes; the server picks the task (usual target guard) and the step
(never guessed: equally fitting steps go back to the user); apply is the same
subtask.* operation as a tap; deleting asks for confirmation; «отмени» restores.
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from student_execution_os.agent import AuthenticatedPrincipal, SQLiteAssistantService
from student_execution_os.domain.clock import FrozenClock
from student_execution_os.domain.errors import AuthorizationDenied, ValidationError
from student_execution_os.domain.model import ActorCategory, HardCutoff, Importance, ObligationCategory
from student_execution_os.persistence import SQLiteCanonicalRepository
from student_execution_os.subtasks import SQLiteSubtaskRepository

NOW = datetime(2026, 10, 7, 9, 0, tzinfo=timezone.utc)


class FixedProvider:
    """A model that proposes exactly the given actions (for negative cases)."""
    name = "fixture"
    model = "fixture"

    def __init__(self, actions):
        self.actions = actions

    def interpret(self, text, context):
        return {"message": "ok", "actions": self.actions(context)}


class AssistantChecklistTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteCanonicalRepository(str(Path(self.tmp.name) / "checklist.sqlite"), clock=FrozenClock(NOW))
        self.repo.initialize()
        self.repo.create_account("a")
        self.repo.create_account("b")
        self.task("task-lab-os-01", "Лаба по ОС")
        self.task("task-physics-1", "Конспект по физике")
        self.steps = SQLiteSubtaskRepository(self.repo)
        self.parser = self.step("task-lab-os-01", "Парсер")
        self.parser_args = self.step("task-lab-os-01", "Парсер аргументов")
        self.tests_parser = self.step("task-lab-os-01", "Написать тесты для парсера")
        self.tests_lexer = self.step("task-lab-os-01", "Написать тесты для лексера")
        self.foreign = self.step("task-physics-1", "Парсер формул")
        self.repo.connection.commit()
        self.service = SQLiteAssistantService(self.repo, AuthenticatedPrincipal("a", "user", "client"))
        self.n = 0

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def task(self, task_id, title, account="a"):
        self.repo.create_task(
            account_id=account, obligation_id=task_id, title=title, description=None,
            category=ObligationCategory.GENERAL, importance=Importance.NORMAL,
            estimated_total_effort_minutes=120, remaining_effort_minutes=120, splittable=True,
            min_chunk_minutes=None, max_chunk_minutes=None, actionable_from=None,
            actual_cutoff=HardCutoff.absent(), target_at=None, actor=ActorCategory.USER_UI)

    def step(self, task_id, title, account="a"):
        self.n = getattr(self, "n", 0) + 1
        return SQLiteSubtaskRepository(self.repo).create(
            account_id=account, subtask_id=f"subtask-fixture-{self.n:03d}", task_id=task_id, title=title,
            effort_minutes=None, position=None, actor=ActorCategory.USER_UI, now=NOW)["id"]

    def apply(self, preview, *, key, confirmed=(), edits=None):
        payload = {"batch_id": preview["batch_id"], "action_ids": [a["id"] for a in preview["actions"]],
                   "idempotency_key": key, "confirmed_action_ids": list(confirmed)}
        if edits:
            payload["edits"] = edits
        return self.service.apply(payload)

    def done(self, subtask_id):
        return self.steps.get("a", subtask_id)["done"]

    # ---- RU / EN phrases ------------------------------------------------------------

    def test_add_step_ru_and_undo(self):
        preview = self.service.interpret('добавь к задаче "лаба" шаг "написать отчёт"')
        action = preview["actions"][0]
        self.assertEqual(action["command"], "CHECKLIST_STEP")
        self.assertEqual(action["payload"]["change"], "ADD")
        self.assertEqual(action["payload"]["obligation_id"], "task-lab-os-01")
        self.assertEqual(action["unresolved_fields"], [])
        result = self.apply(preview, key="add-ru")
        created = result["results"][0]
        self.assertEqual(created["operation"], "subtask.create")
        titles = [s["title"] for s in self.steps.for_task("a", "task-lab-os-01")]
        self.assertIn("написать отчёт", titles)
        # [Отменить] deletes exactly the step it added.
        self.service.undo({"idempotency_key": "add-ru-undo", "apply_idempotency_key": "add-ru"})
        self.assertNotIn("написать отчёт", [s["title"] for s in self.steps.for_task("a", "task-lab-os-01")])

    def test_add_step_en(self):
        preview = self.service.interpret('add step "write the report" to task "лаба"')
        self.assertEqual(preview["actions"][0]["payload"]["change"], "ADD")
        self.apply(preview, key="add-en")
        self.assertIn("write the report", [s["title"] for s in self.steps.for_task("a", "task-lab-os-01")])

    def test_complete_then_no_put_it_back(self):
        preview = self.service.interpret('отметь в лабе шаг "парсер" выполненным')
        action = preview["actions"][0]
        self.assertEqual(action["payload"]["change"], "COMPLETE")
        # «Парсер» fits exactly; «Парсер аргументов» only half: no question needed.
        self.assertEqual(action["payload"]["subtask_id"], self.parser)
        self.assertEqual(action["resolution"]["step"]["title"], "Парсер")
        self.apply(preview, key="complete")
        self.assertTrue(self.done(self.parser))
        self.assertFalse(self.done(self.parser_args))
        # «нет, верни этот шаг» right after: the same step, reopened.
        follow = self.service.interpret("нет, верни этот шаг", {"previous_batch_id": preview["batch_id"]})
        again = follow["actions"][0]
        self.assertEqual((again["payload"]["change"], again["payload"]["subtask_id"]), ("REOPEN", self.parser))
        self.apply(follow, key="reopen")
        self.assertFalse(self.done(self.parser))

    def test_complete_en_and_undo_restores(self):
        preview = self.service.interpret('mark step "Парсер" in "лаба" as done')
        self.apply(preview, key="complete-en")
        self.assertTrue(self.done(self.parser))
        self.service.undo({"idempotency_key": "complete-en-undo", "apply_idempotency_key": "complete-en"})
        self.assertFalse(self.done(self.parser))

    def test_phrases_ru_en_map_to_the_typed_change(self):
        from student_execution_os.agent import checklist_actions
        items = [{"id": "task-lab-os-01", "kind": "TASK", "title": "Лаба по ОС", "version": 1, "status": "ACTIVE"},
                 {"id": "task-lab-en-01", "kind": "TASK", "title": "Lab report", "version": 1, "status": "ACTIVE"}]
        for text, change, step in (
            ('Добавь в задачу «Лаба по ОС» пункт «введение»', "ADD", "введение"),
            ('добавь шаг «парсер» к задаче «лаба»', "ADD", "парсер"),
            ('верни шаг «парсер» в лабе', "REOPEN", "парсер"),
            ('удали шаг «парсер» из лабы', "DELETE", "парсер"),
            ('add step "write tests" to task "lab report"', "ADD", "write tests"),
            ('uncheck step "parser" in "lab report"', "REOPEN", "parser"),
        ):
            action = checklist_actions.parse(text, items)
            self.assertIsNotNone(action, text)
            payload = action["payload"]
            self.assertEqual(payload["change"], change, text)
            self.assertEqual(payload.get("title") or payload.get("step_text"), step, text)
            self.assertEqual(action["requires_confirmation"], change == "DELETE", text)
        for text in ("Купить хлеб", "отметь зарядку", "шаг за шагом разобрать конспект", "добавь задачу «лаба»"):
            self.assertIsNone(checklist_actions.parse(text, items), text)
        # Without a matching follow-up context, «верни этот шаг» is not guessed.
        self.assertIsNone(checklist_actions.follow_up("нет, верни этот шаг", None, items))
        self.assertIsNone(checklist_actions.follow_up("нет, верни этот шаг", {"previous_actions": []}, items))

    # ---- never guess ----------------------------------------------------------------

    def test_equally_fitting_steps_are_asked_not_guessed(self):
        preview = self.service.interpret('отметь в лабе шаг "написать тесты" выполненным')
        action = preview["actions"][0]
        self.assertIn("subtask_id", action["unresolved_fields"])
        self.assertNotIn("subtask_id", action["payload"])
        self.assertEqual({c["id"] for c in action["resolution"]["step_candidates"]},
                         {self.tests_parser, self.tests_lexer})
        with self.assertRaises(ValidationError):
            self.apply(preview, key="ambiguous")
        self.assertFalse(self.done(self.tests_parser) or self.done(self.tests_lexer))
        # The user picks one in the preview: only that step changes.
        self.apply(preview, key="picked", edits={action["id"]: {"subtask_id": self.tests_lexer}})
        self.assertTrue(self.done(self.tests_lexer))
        self.assertFalse(self.done(self.tests_parser))

    def test_ambiguous_task_is_left_for_the_user(self):
        self.task("task-lab-net-1", "Лаба по сетям")
        self.repo.connection.commit()
        preview = self.service.interpret('добавь к задаче "лаба" шаг "написать тесты"')
        action = preview["actions"][0]
        self.assertIn("target", action["unresolved_fields"])
        self.assertNotIn("obligation_id", action["payload"])
        with self.assertRaises(ValidationError):
            self.apply(preview, key="which-lab")

    def test_unknown_step_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.service.interpret('отметь в лабе шаг "дизассемблер" выполненным')

    def test_delete_needs_confirmation(self):
        preview = self.service.interpret('удали шаг "парсер аргументов" из лабы')
        action = preview["actions"][0]
        self.assertEqual(action["payload"]["change"], "DELETE")
        self.assertTrue(action["requires_confirmation"])
        with self.assertRaises(AuthorizationDenied):
            self.apply(preview, key="delete-unconfirmed")
        self.assertIn(self.parser_args, [s["id"] for s in self.steps.for_task("a", "task-lab-os-01")])
        self.apply(preview, key="delete", confirmed=[action["id"]])
        self.assertNotIn(self.parser_args, [s["id"] for s in self.steps.for_task("a", "task-lab-os-01")])

    # ---- a model cannot reach outside the task --------------------------------------

    def model(self, *actions):
        return SQLiteAssistantService(self.repo, AuthenticatedPrincipal("a", "user", "client"),
                                      provider=FixedProvider(lambda context: list(actions)))

    def test_model_cannot_address_a_step_of_another_task(self):
        version = self.repo.get_task("a", "task-lab-os-01").obligation.version
        service = self.model({"command": "CHECKLIST_STEP", "payload": {
            "obligation_id": "task-lab-os-01", "change": "COMPLETE", "subtask_id": self.foreign},
            "confidence": 0.9, "unresolved_fields": [], "expected_version": version, "requires_confirmation": False})
        with self.assertRaises(ValidationError):
            service.interpret("отметь в лабе по ОС парсер формул")
        self.assertFalse(self.done(self.foreign))

    def test_model_payload_shape_is_checked(self):
        version = self.repo.get_task("a", "task-lab-os-01").obligation.version
        for payload in (
            {"obligation_id": "task-lab-os-01", "change": "REORDER", "step_text": "парсер"},
            {"obligation_id": "task-lab-os-01", "change": "ADD"},
            {"obligation_id": "task-lab-os-01", "change": "ADD", "title": "x", "subtask_id": self.parser},
            {"obligation_id": "task-lab-os-01", "change": "COMPLETE"},
            {"obligation_id": "task-lab-os-01", "change": "SET_EFFORT", "step_text": "парсер", "effort_minutes": -5},
        ):
            service = self.model({"command": "CHECKLIST_STEP", "payload": payload, "confidence": 0.9,
                                  "unresolved_fields": [], "expected_version": version, "requires_confirmation": False})
            with self.assertRaises(ValidationError, msg=payload):
                service.interpret("в лабе по ОС: парсер")

    def test_model_rename_and_effort_go_through_subtask_update(self):
        version = self.repo.get_task("a", "task-lab-os-01").obligation.version
        service = self.model(
            {"command": "CHECKLIST_STEP", "payload": {"obligation_id": "task-lab-os-01", "change": "RENAME",
                                                      "step_text": "парсер аргументов", "title": "Разбор аргументов"},
             "confidence": 0.9, "unresolved_fields": [], "expected_version": version, "requires_confirmation": False},
            {"command": "CHECKLIST_STEP", "payload": {"obligation_id": "task-lab-os-01", "change": "SET_EFFORT",
                                                      "subtask_id": self.parser, "effort_minutes": 40},
             "confidence": 0.9, "unresolved_fields": [], "expected_version": version, "requires_confirmation": False})
        preview = service.interpret("в лабе по ОС переименуй парсер аргументов и оцени парсер")
        service.apply({"batch_id": preview["batch_id"], "action_ids": [a["id"] for a in preview["actions"]],
                       "idempotency_key": "rename-effort"})
        self.assertEqual(self.steps.get("a", self.parser_args)["title"], "Разбор аргументов")
        self.assertEqual(self.steps.get("a", self.parser)["effort_minutes"], 40)
        # The task itself is untouched: the Assistant does not own the checklist's task.
        self.assertEqual(self.repo.get_task("a", "task-lab-os-01").obligation.version, version)


if __name__ == "__main__":
    unittest.main()
