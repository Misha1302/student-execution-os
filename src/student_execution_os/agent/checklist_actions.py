"""Assistant edits of a Task's checklist (schema v33, ADR 0036).

The Assistant does not own checklist state. It proposes one typed CHECKLIST_STEP
action on one Task (the Task is an ordinary Assistant target, with the usual
ambiguity guard), the server decides which step the user meant, and apply runs the
same subtask.* sync operation as a tap in the checklist. A step reference that fits
several steps equally is left unresolved with exactly those steps for the user to
pick — never guessed. Deleting a step always asks for confirmation.
"""
from __future__ import annotations

import re
from typing import Any

from student_execution_os.domain.errors import ValidationError
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository
from student_execution_os.subtasks import SQLiteSubtaskRepository

from .disambiguation import MAX_CANDIDATES, title_score, tokens
from .model import AgentCommand

CHANGES = ("ADD", "RENAME", "COMPLETE", "REOPEN", "DELETE", "SET_EFFORT")
FIELDS = {"change", "step_text", "subtask_id", "title", "effort_minutes"}
# Which steps a change naturally addresses when the words fit several.
_PREFERRED_STATE = {"COMPLETE": False, "REOPEN": True}


def validate(payload: dict[str, Any], unresolved: list[str]) -> None:
    change = payload.get("change")
    if change not in CHANGES:
        raise ValidationError("assistant checklist change must be one of " + ", ".join(CHANGES))
    if change in {"ADD", "RENAME"} and not payload.get("title") and "title" not in unresolved:
        raise ValidationError(f"assistant checklist {change} needs the step title")
    if change == "SET_EFFORT" and "effort_minutes" not in payload:
        raise ValidationError("assistant checklist SET_EFFORT needs effort_minutes (null clears it)")
    effort = payload.get("effort_minutes")
    if effort is not None and (isinstance(effort, bool) or not isinstance(effort, int) or not 0 < effort <= 100_000):
        raise ValidationError("assistant checklist effort_minutes must be a positive whole number of minutes")
    text = payload.get("step_text")
    if text is not None and (not isinstance(text, str) or not text.strip() or len(text) > 300):
        raise ValidationError("assistant step_text must be short text")
    if change != "ADD" and not payload.get("subtask_id") and not text and "subtask_id" not in unresolved:
        raise ValidationError(f"assistant checklist {change} needs the step (subtask_id or step_text)")
    if change == "ADD" and payload.get("subtask_id"):
        raise ValidationError("assistant checklist ADD creates a new step; it cannot name an existing one")


def _brief(step: dict[str, Any]) -> dict[str, Any]:
    return {"id": step["id"], "title": step["title"], "done": step["done"]}


def resolve_step(canonical: SQLiteCanonicalRepository, account_id: str, payload: dict[str, Any],
                 unresolved: list[str]) -> dict[str, Any] | None:
    """Pins ``subtask_id`` to one step of the target Task, or leaves it for the user.

    Returns the resolution shown in the preview: the step, or the equally fitting
    candidates (``subtask_id`` is then added to ``unresolved``).
    """
    change = payload["change"]
    task_id = payload.get("obligation_id")
    if change == "ADD" or not task_id:
        return None
    steps = SQLiteSubtaskRepository(canonical).for_task(account_id, str(task_id))
    if payload.get("subtask_id"):
        step = next((item for item in steps if item["id"] == payload["subtask_id"]), None)
        if step is None:
            # Never let a model address a step of another task or account.
            raise ValidationError("assistant proposal references a step outside this task")
        return {"step": _brief(step)}
    if "subtask_id" in unresolved:
        return None
    said = tokens(payload.get("step_text") or "")
    if not said:
        raise ValidationError("assistant step_text does not name a step")
    scored = [(title_score(step["title"], said), step) for step in steps]
    scored = [(score, step) for score, step in scored if score[0] > 0]
    if not scored:
        raise ValidationError("this task has no step like that")
    best = max(score for score, _ in scored)
    top = [step for score, step in scored if score == best]
    preferred = _PREFERRED_STATE.get(change)
    if preferred is not None and len(top) > 1:
        fitting = [step for step in top if step["done"] is preferred]
        top = fitting or top
    if len(top) == 1:
        payload["subtask_id"] = top[0]["id"]
        return {"step": _brief(top[0])}
    unresolved.append("subtask_id")
    return {"step_candidates": [_brief(step) for step in top[:MAX_CANDIDATES]]}


def operation(data: dict[str, Any], new_id) -> tuple[str, str, dict[str, Any]]:
    """The subtask.* sync operation a checklist action stands for (same as the buttons)."""
    change = data["change"]
    if change == "ADD":
        body: dict[str, Any] = {"task_id": data["obligation_id"], "title": data["title"]}
        if data.get("effort_minutes") is not None:
            body["effort_minutes"] = data["effort_minutes"]
        return "subtask.create", new_id("subtask"), body
    step = str(data["subtask_id"])
    if change == "RENAME":
        return "subtask.update", step, {"title": data["title"]}
    if change == "SET_EFFORT":
        return "subtask.update", step, {"effort_minutes": data.get("effort_minutes")}
    if change == "COMPLETE":
        return "subtask.complete", step, {}
    if change == "REOPEN":
        return "subtask.reopen", step, {}
    return "subtask.delete", step, {}


def inverse(canonical: SQLiteCanonicalRepository, account_id: str, data: dict[str, Any]) -> dict[str, Any] | None:
    """How «отмени» restores the step (taken before apply). A deletion is not undoable."""
    change = data["change"]
    if change in {"ADD", "DELETE"} or not data.get("subtask_id"):
        return None  # ADD: the creation inverse deletes it; DELETE asks for confirmation instead
    step = SQLiteSubtaskRepository(canonical).get(account_id, str(data["subtask_id"]))
    if change == "COMPLETE":
        return None if step["done"] else {"operation": "subtask.reopen", "payload": {}}
    if change == "REOPEN":
        return None if not step["done"] else {"operation": "subtask.complete", "payload": {}}
    if change == "RENAME":
        return {"operation": "subtask.update", "payload": {"title": step["title"]}}
    return {"operation": "subtask.update", "payload": {"effort_minutes": step["effort_minutes"]}}


def context_steps(canonical: SQLiteCanonicalRepository, account_id: str, task_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    """Checklist steps of the tasks the model may address (titles and state only)."""
    store = SQLiteSubtaskRepository(canonical)
    out: dict[str, list[dict[str, Any]]] = {}
    for task_id in task_ids:
        steps = store.for_task(account_id, task_id)
        if steps:
            out[task_id] = [_brief(step) for step in steps[:30]]
    return out


# ---- deterministic RU/EN phrases (no model needed) ---------------------------------

_Q = r"[«\"“']"
_QE = r"[»\"”']"
_ADD = re.compile(
    rf"^(?:добавь|добавить|add)\s+(?:в\s+|к\s+|to\s+)?(?:задач[еуи]|task\s+)?\s*{_Q}(?P<task>[^»\"”']+){_QE}\s*"
    rf"(?:шаг|пункт|step|item)\s+{_Q}(?P<step>[^»\"”']+){_QE}\s*$", re.IGNORECASE)
_ADD_STEP_FIRST = re.compile(
    rf"^(?:добавь|добавить|add)\s+(?:шаг|пункт|step|item)\s+{_Q}(?P<step>[^»\"”']+){_QE}\s+"
    rf"(?:в|к|to)\s+(?:задач[еуи]\s+|task\s+)?{_Q}(?P<task>[^»\"”']+){_QE}\s*$", re.IGNORECASE)
_MARK = re.compile(
    rf"^(?P<verb>отметь|отметить|закрой|mark|check off|верни|открой снова|снова открой|reopen|uncheck|удали|delete|remove)\s+"
    rf"(?:в\s+(?:задаче\s+)?(?P<task1>{_Q}[^»\"”']+{_QE}|\S+)\s+)?(?:шаг|пункт|step|item)\s+{_Q}(?P<step>[^»\"”']+){_QE}"
    rf"(?:\s+(?:в|из|from|in)\s+(?:задач[еиу]\s+)?(?P<task2>{_Q}[^»\"”']+{_QE}|\S+))?"
    rf"(?:\s+(?:выполненным|сделанным|готовым|as done|done))?\s*$", re.IGNORECASE)
_FOLLOW_UP_REOPEN = re.compile(
    r"^(?:нет[,!]?\s+)?(?:верни|открой снова|снова открой|отмени отметку)\s+(?:этот\s+|его\s+)?(?:шаг|пункт)\s*\.?$"
    r"|^(?:no[,!]?\s+)?(?:reopen|uncheck)\s+(?:this|that|it)(?:\s+step)?\s*\.?$", re.IGNORECASE)


def _strip(value: str | None) -> str | None:
    if value is None:
        return None
    return value.strip().strip("«»\"“”'").strip() or None


def _action(change: str, task_text: str, payload: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, Any]:
    from .commands import match_target  # the same task matcher the other local commands use

    task, _candidates = match_target(task_text, items, kinds=("TASK",), statuses=("ACTIVE", "DRAFT"))
    action = {
        "command": AgentCommand.CHECKLIST_STEP.value,
        "payload": {"change": change, **payload},
        "confidence": 0.9 if task else 0.5,
        "unresolved_fields": [],
        "expected_version": None,
        "requires_confirmation": change == "DELETE",
    }
    if task is None:
        # Several tasks fit, or none: the user picks the task in the preview.
        action["payload"]["target_text"] = task_text
        action["unresolved_fields"] = ["target", "expected_version"]
    else:
        action["payload"]["obligation_id"] = task["id"]
        action["expected_version"] = int(task["version"])
    return action


def parse(text: str, items: list[dict[str, Any]]) -> dict[str, Any] | None:
    """«добавь к задаче "лаба" шаг "написать тесты"», «отметь в лабе шаг "парсер" выполненным»."""
    clean = " ".join(str(text or "").strip().split())
    for pattern in (_ADD, _ADD_STEP_FIRST):
        match = pattern.match(clean)
        if match:
            return _action("ADD", _strip(match.group("task")) or "", {"title": _strip(match.group("step"))}, items)
    match = _MARK.match(clean)
    if match:
        task = _strip(match.group("task1") or match.group("task2"))
        if not task:
            return None
        verb = match.group("verb").casefold()
        change = ("REOPEN" if verb in {"верни", "открой снова", "снова открой", "reopen", "uncheck"}
                  else "DELETE" if verb in {"удали", "delete", "remove"} else "COMPLETE")
        return _action(change, task, {"step_text": _strip(match.group("step"))}, items)
    return None


def follow_up(text: str, session: dict[str, Any] | None, items: list[dict[str, Any]]) -> dict[str, Any] | None:
    """«нет, верни этот шаг» right after the Assistant checked a step off: reopen that step.

    Only when the previous turn established exactly one checked-off step; otherwise
    the follow-up stays for the language model or the user.
    """
    if not session or not _FOLLOW_UP_REOPEN.match(" ".join(str(text or "").strip().split())):
        return None
    steps = [action for action in session.get("previous_actions") or []
             if action.get("command") == AgentCommand.CHECKLIST_STEP.value
             and (action.get("payload") or {}).get("change") == "COMPLETE"
             and (action.get("payload") or {}).get("subtask_id") and (action.get("payload") or {}).get("obligation_id")]
    if len(steps) != 1:
        return None
    previous = steps[0]["payload"]
    task = next((item for item in items if item.get("id") == previous["obligation_id"]), None)
    if task is None:
        return None
    return {
        "command": AgentCommand.CHECKLIST_STEP.value,
        "payload": {"change": "REOPEN", "obligation_id": previous["obligation_id"], "subtask_id": previous["subtask_id"]},
        "confidence": 0.95,
        "unresolved_fields": [],
        "expected_version": int(task["version"]),
        "requires_confirmation": False,
    }
