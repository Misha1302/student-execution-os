"""Projects, their member tasks and milestones."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from student_execution_os.domain.errors import EntityNotFound, ValidationError
from student_execution_os.domain.model import (
    ActorCategory,
    Importance,
    MilestoneOwnerKind,
    MilestoneRole,
    MilestoneStatus,
)
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository, _iso
from student_execution_os.planning.state import SQLitePlanningStateSource

from ..serialize import task_payload

from ..primitives import APPLIED, NOOP, _ID, Outcome, parse_instant, _title, _description

from .base import CommandHandler, Handler
from .tasks import TaskCommandHandler


class ProjectCommandHandler(CommandHandler):
    """Projects, their member tasks and milestones."""

    def __init__(self, repo: SQLiteCanonicalRepository, *, account_id: str, actor: ActorCategory,
                 now: datetime, tasks: TaskCommandHandler) -> None:
        super().__init__(repo, account_id=account_id, actor=actor, now=now)
        self.tasks = tasks

    def operations(self) -> dict[str, Handler]:
        return {
            "project.create": self.project_create,
            "project.update": self.project_update,
            "project.complete": self.project_complete,
            "project.cancel": self.project_cancel,
            "project.reopen": self.project_reopen,
            "project.task.create": self.project_task_create,
            "project.member.add": self.project_member_add,
            "project.member.remove": self.project_member_remove,
            "milestone.create": self.milestone_create,
            "milestone.update": self.milestone_update,
            "milestone.complete": self.milestone_complete,
            "milestone.cancel": self.milestone_cancel,
            "milestone.reopen": self.milestone_reopen,
            "milestone.delete": self.milestone_delete,
        }

    @staticmethod
    def _project_out(project) -> dict[str, Any]:
        return {
            "id": project.id,
            "title": project.title,
            "description": project.description,
            "status": project.status.value,
            "importance": project.importance.value if project.importance else None,
            "version": project.version,
            "created_at": _iso(project.created_at),
            "updated_at": _iso(project.updated_at),
        }

    @staticmethod
    def _milestone_out(item) -> dict[str, Any]:
        return {
            "id": item.id,
            "owner_kind": item.owner_kind.value,
            "owner_id": item.owner_id,
            "title": item.title,
            "marker_at": _iso(item.marker_at),
            "role": item.role.value,
            "consequence": item.consequence,
            "hard_for_planning": item.hard_for_planning,
            "status": item.status.value,
            "version": item.version,
        }

    def project_create(self, project_id: str, payload: dict[str, Any]) -> Outcome:
        if not _ID.match(project_id):
            raise ValidationError("project id must be a client-generated identifier (8-128 safe characters)")
        existing = self.repo.connection.execute(
            "SELECT account_id FROM projects WHERE id=?", (project_id,)
        ).fetchone()
        if existing is not None:
            if existing["account_id"] != self.account_id:
                raise ValidationError("project id is already in use")
            return Outcome(NOOP, self._project_out(self.repo.get_project(self.account_id, project_id)), "ALREADY_EXISTS")
        allowed = {"title", "description", "importance"}
        unknown = set(payload) - allowed
        if unknown:
            raise ValidationError("project fields are not supported: " + ", ".join(sorted(unknown)))
        importance = payload.get("importance")
        project = self.repo.create_project(
            account_id=self.account_id,
            project_id=project_id,
            title=_title(payload.get("title")),
            description=_description(payload.get("description")),
            importance=Importance(importance) if importance else None,
            actor=self.actor,
        )
        return Outcome(APPLIED, self._project_out(project))

    def project_update(self, project_id: str, payload: dict[str, Any]) -> Outcome:
        allowed = {"title", "description", "importance", "expected_version"}
        unknown = set(payload) - allowed
        if unknown:
            raise ValidationError("project update fields are not supported: " + ", ".join(sorted(unknown)))
        current = self.repo.get_project(self.account_id, project_id)
        kwargs: dict[str, Any] = {}
        if "title" in payload:
            kwargs["title"] = _title(payload["title"])
        if "description" in payload:
            kwargs["description"] = _description(payload["description"])
        if "importance" in payload:
            kwargs["importance"] = Importance(payload["importance"]) if payload["importance"] else None
        updated = self.repo.update_project(
            account_id=self.account_id, project_id=project_id,
            expected_version=int(payload.get("expected_version") or current.version),
            actor=self.actor, **kwargs,
        )
        return Outcome(APPLIED, self._project_out(updated))

    def _project_transition(self, project_id: str, payload: dict[str, Any], action: str) -> Outcome:
        if set(payload) - {"expected_version"}:
            raise ValidationError(f"project.{action} only accepts expected_version")
        current = self.repo.get_project(self.account_id, project_id)
        method = {
            "complete": self.repo.complete_project,
            "cancel": self.repo.cancel_project,
            "reopen": self.repo.reopen_project,
        }[action]
        if (
            (action == "complete" and current.status.value == "COMPLETED")
            or (action == "cancel" and current.status.value == "CANCELLED")
            or (action == "reopen" and current.status.value == "ACTIVE")
        ):
            return Outcome(NOOP, self._project_out(current), "ALREADY_IN_STATE")
        updated = method(
            account_id=self.account_id, project_id=project_id,
            expected_version=int(payload.get("expected_version") or current.version),
            actor=self.actor,
        )
        return Outcome(APPLIED, self._project_out(updated))

    def project_complete(self, project_id: str, payload: dict[str, Any]) -> Outcome:
        return self._project_transition(project_id, payload, "complete")

    def project_cancel(self, project_id: str, payload: dict[str, Any]) -> Outcome:
        return self._project_transition(project_id, payload, "cancel")

    def project_reopen(self, project_id: str, payload: dict[str, Any]) -> Outcome:
        return self._project_transition(project_id, payload, "reopen")

    def project_task_create(self, project_id: str, payload: dict[str, Any]) -> Outcome:
        task_id = str(payload.get("task_id") or "")
        if not _ID.match(task_id):
            raise ValidationError("project task requires a client-generated task_id")
        task_payload = {k: v for k, v in payload.items() if k != "task_id"}
        task_result = self.tasks.task_create(task_id, task_payload)
        project = self.repo.get_project(self.account_id, project_id)
        exists = self.repo.connection.execute(
            "SELECT 1 FROM project_members WHERE account_id=? AND project_id=? AND obligation_id=?",
            (self.account_id, project_id, task_id),
        ).fetchone()
        if exists is None:
            project = self.repo.add_project_member(
                account_id=self.account_id, project_id=project_id, obligation_id=task_id,
                expected_version=project.version, actor=self.actor,
            )
        return Outcome(APPLIED, {"project": self._project_out(project), "task": task_result.entity})

    def project_member_add(self, project_id: str, payload: dict[str, Any]) -> Outcome:
        if set(payload) - {"obligation_id"}:
            raise ValidationError("project.member.add only accepts obligation_id")
        obligation_id = str(payload.get("obligation_id") or "")
        current = self.repo.get_project(self.account_id, project_id)
        exists = self.repo.connection.execute(
            "SELECT 1 FROM project_members WHERE account_id=? AND project_id=? AND obligation_id=?",
            (self.account_id, project_id, obligation_id),
        ).fetchone()
        if exists is not None:
            return Outcome(NOOP, self._project_out(current), "ALREADY_MEMBER")
        updated = self.repo.add_project_member(
            account_id=self.account_id, project_id=project_id, obligation_id=obligation_id,
            expected_version=current.version, actor=self.actor,
        )
        return Outcome(APPLIED, self._project_out(updated))

    def project_member_remove(self, project_id: str, payload: dict[str, Any]) -> Outcome:
        if set(payload) - {"obligation_id"}:
            raise ValidationError("project.member.remove only accepts obligation_id")
        obligation_id = str(payload.get("obligation_id") or "")
        current = self.repo.get_project(self.account_id, project_id)
        updated = self.repo.remove_project_member(
            account_id=self.account_id, project_id=project_id, obligation_id=obligation_id,
            expected_version=current.version, actor=self.actor,
        )
        return Outcome(APPLIED, self._project_out(updated))

    def milestone_create(self, milestone_id: str, payload: dict[str, Any]) -> Outcome:
        if not _ID.match(milestone_id):
            raise ValidationError("milestone id must be a client-generated identifier")
        allowed = {"project_id", "title", "marker_at", "role", "consequence", "hard_for_planning"}
        unknown = set(payload) - allowed
        if unknown:
            raise ValidationError("milestone fields are not supported: " + ", ".join(sorted(unknown)))
        marker = parse_instant(payload.get("marker_at"), "marker_at")
        if marker is None:
            raise ValidationError("marker_at is required")
        existing = self.repo.connection.execute(
            "SELECT account_id FROM milestones WHERE id=?", (milestone_id,)
        ).fetchone()
        if existing is not None:
            if existing["account_id"] != self.account_id:
                raise ValidationError("milestone id is already in use")
            row = next(m for m in SQLitePlanningStateSource(self.repo).list_milestones(self.account_id) if m.id == milestone_id)
            return Outcome(NOOP, self._milestone_out(row), "ALREADY_EXISTS")
        item = self.repo.create_milestone(
            account_id=self.account_id,
            milestone_id=milestone_id,
            owner_kind=MilestoneOwnerKind.PROJECT,
            owner_id=str(payload.get("project_id") or ""),
            title=_title(payload.get("title")),
            marker_at=marker,
            role=MilestoneRole(payload.get("role") or MilestoneRole.INTERMEDIATE.value),
            consequence=_description(payload.get("consequence")),
            hard_for_planning=bool(payload.get("hard_for_planning", False)),
            actor=self.actor,
        )
        return Outcome(APPLIED, self._milestone_out(item))

    def milestone_update(self, milestone_id: str, payload: dict[str, Any]) -> Outcome:
        allowed = {"title", "marker_at", "expected_version"}
        unknown = set(payload) - allowed
        if unknown:
            raise ValidationError("milestone update fields are not supported: " + ", ".join(sorted(unknown)))
        row = self.repo.connection.execute(
            "SELECT version FROM milestones WHERE account_id=? AND id=?", (self.account_id, milestone_id)
        ).fetchone()
        if row is None:
            raise EntityNotFound("milestone not found")
        marker = parse_instant(payload.get("marker_at"), "marker_at")
        item = self.repo.update_milestone(
            account_id=self.account_id, milestone_id=milestone_id,
            expected_version=int(payload.get("expected_version") or row["version"]),
            actor=self.actor, marker_at=marker,
            title=_title(payload["title"]) if "title" in payload else None,
        )
        return Outcome(APPLIED, self._milestone_out(item))

    def _milestone_status(self, milestone_id: str, payload: dict[str, Any], status: MilestoneStatus) -> Outcome:
        if set(payload) - {"expected_version"}:
            raise ValidationError("milestone lifecycle only accepts expected_version")
        row = self.repo.connection.execute(
            "SELECT status,version FROM milestones WHERE account_id=? AND id=?", (self.account_id, milestone_id)
        ).fetchone()
        if row is None:
            raise EntityNotFound("milestone not found")
        if row["status"] == status.value:
            item = next(m for m in SQLitePlanningStateSource(self.repo).list_milestones(self.account_id) if m.id == milestone_id)
            return Outcome(NOOP, self._milestone_out(item), "ALREADY_IN_STATE")
        item = self.repo.set_milestone_status(
            account_id=self.account_id, milestone_id=milestone_id,
            expected_version=int(payload.get("expected_version") or row["version"]),
            status=status, actor=self.actor,
        )
        return Outcome(APPLIED, self._milestone_out(item))

    def milestone_complete(self, milestone_id: str, payload: dict[str, Any]) -> Outcome:
        return self._milestone_status(milestone_id, payload, MilestoneStatus.COMPLETED)

    def milestone_cancel(self, milestone_id: str, payload: dict[str, Any]) -> Outcome:
        return self._milestone_status(milestone_id, payload, MilestoneStatus.CANCELLED)

    def milestone_reopen(self, milestone_id: str, payload: dict[str, Any]) -> Outcome:
        return self._milestone_status(milestone_id, payload, MilestoneStatus.ACTIVE)

    def milestone_delete(self, milestone_id: str, payload: dict[str, Any]) -> Outcome:
        if set(payload) - {"expected_version"}:
            raise ValidationError("milestone.delete only accepts expected_version")
        row = self.repo.connection.execute(
            "SELECT version FROM milestones WHERE account_id=? AND id=?", (self.account_id, milestone_id)
        ).fetchone()
        if row is None:
            raise EntityNotFound("milestone not found")
        self.repo.delete_milestone(
            account_id=self.account_id, milestone_id=milestone_id,
            expected_version=int(payload.get("expected_version") or row["version"]), actor=self.actor,
        )
        return Outcome(APPLIED, {"kind": "MILESTONE", "id": milestone_id, "deleted": True})
