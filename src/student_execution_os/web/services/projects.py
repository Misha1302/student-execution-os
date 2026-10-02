"""Projects with their tasks and milestones."""
from __future__ import annotations

from typing import Any

from student_execution_os.domain.errors import EntityNotFound
from student_execution_os.persistence import extras
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository
from student_execution_os.reminders import ReminderStore
from student_execution_os.planning import PlanningService, SQLitePlanningStateSource

from .common import _jsonify

from .base import ApplicationService


class ProjectQueries(ApplicationService):
    """Projects with their tasks and milestones."""

    def _projects_payload(self, repo: SQLiteCanonicalRepository) -> list[dict[str, Any]]:
        source = SQLitePlanningStateSource(repo)
        task_models = source.list_tasks(self.account_id)
        task_risks: dict[str, Any] = {}
        try:
            snapshot = self._snapshot(repo, hours=24 * 14, output_hours=24 * 14)
            task_risks = {r.task_id: r for r in PlanningService().build(snapshot, now=self._now()).risks}
        except Exception:
            task_risks = {}
        reminders = ReminderStore(repo).pending_reminders(self.account_id)
        counts = extras.progress_counts(repo, self.account_id)
        task_payloads = {
            task.obligation.id: self._task(
                task, risk=task_risks.get(task.obligation.id),
                remind_at=reminders.get(task.obligation.id),
                count=counts.get(task.obligation.id),
            )
            for task in task_models
        }
        event_payloads = {
            event.obligation.id: self._event(event)
            for event in source.list_events(self.account_id)
        }
        milestones = source.list_milestones(self.account_id)
        milestones_by_project: dict[str, list[dict[str, Any]]] = {}
        for item in milestones:
            if item.owner_kind.value != "PROJECT":
                continue
            milestones_by_project.setdefault(item.owner_id, []).append({
                "id": item.id,
                "title": item.title,
                "marker_at": _jsonify(item.marker_at),
                "role": item.role.value,
                "consequence": item.consequence,
                "hard_for_planning": item.hard_for_planning,
                "status": item.status.value,
                "version": item.version,
                "overdue": item.status.value == "ACTIVE" and item.marker_at < self._now(),
            })
        risk_rank = {
            "UNKNOWN": 0, "NOT_APPLICABLE": 1, "SAFE": 2, "START_SOON": 3,
            "AT_RISK": 4, "CRITICAL": 5, "IMPOSSIBLE": 6, "OVERDUE": 7,
        }
        rows = repo.connection.execute(
            "SELECT * FROM projects WHERE account_id=? ORDER BY "
            "CASE status WHEN 'ACTIVE' THEN 0 WHEN 'COMPLETED' THEN 1 WHEN 'CANCELLED' THEN 2 ELSE 3 END,"
            "updated_at DESC,id",
            (self.account_id,),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            member_rows = repo.connection.execute(
                "SELECT obligation_id FROM project_members WHERE account_id=? AND project_id=? ORDER BY obligation_id",
                (self.account_id, row["id"]),
            ).fetchall()
            members: list[dict[str, Any]] = []
            member_tasks: list[dict[str, Any]] = []
            for member in member_rows:
                oid = member["obligation_id"]
                if oid in task_payloads:
                    payload = {**task_payloads[oid], "kind": "TASK"}
                    member_tasks.append(payload)
                    members.append(payload)
                elif oid in event_payloads:
                    members.append({**event_payloads[oid], "kind": "EVENT"})
            relevant_tasks = [t for t in member_tasks if t["status"] != "CANCELLED"]
            known_effort = [t for t in relevant_tasks if t["estimated_total_effort_minutes"] is not None]
            total_effort = sum(int(t["estimated_total_effort_minutes"]) for t in known_effort)
            remaining = sum(
                int(t["remaining_effort_minutes"] or 0)
                for t in known_effort if t["status"] not in {"COMPLETED", "CANCELLED", "ARCHIVED"}
            )
            if total_effort:
                progress = max(0, min(100, round((total_effort - remaining) * 100 / total_effort)))
                progress_basis = "EFFORT"
            elif relevant_tasks:
                done = sum(1 for t in relevant_tasks if t["status"] == "COMPLETED")
                progress = round(done * 100 / len(relevant_tasks))
                progress_basis = "TASK_COUNT"
            else:
                progress = 0
                progress_basis = "EMPTY"
            child_risks = [
                t["risk"] for t in relevant_tasks
                if t.get("risk") and t["status"] in {"ACTIVE", "DRAFT"}
            ]
            risk = max(child_risks, key=lambda r: risk_rank.get(r["state"], 0), default=None)
            project_milestones = sorted(
                milestones_by_project.get(row["id"], []),
                key=lambda item: (item["status"] != "ACTIVE", item["marker_at"], item["id"]),
            )
            if row["status"] == "ACTIVE" and any(m["overdue"] for m in project_milestones):
                if risk is None or risk_rank.get(risk["state"], 0) < risk_rank["OVERDUE"]:
                    risk = {
                        "state": "OVERDUE",
                        "basis": "PROJECT_MILESTONE",
                        "reasons": ["PROJECT_MILESTONE_OVERDUE"],
                        "latest_safe_start": None,
                        "policy_version": "project-derived-v1",
                    }
            out.append({
                "id": row["id"],
                "title": row["title"],
                "description": row["description"],
                "status": row["status"],
                "importance": row["importance"],
                "version": int(row["version"]),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "members": members,
                "milestones": project_milestones,
                "progress": {
                    "percent": progress,
                    "basis": progress_basis,
                    "tasks_total": len(relevant_tasks),
                    "tasks_completed": sum(1 for t in relevant_tasks if t["status"] == "COMPLETED"),
                    "estimated_total_effort_minutes": total_effort or None,
                    "remaining_effort_minutes": remaining if total_effort else None,
                },
                "risk": risk,
            })
        return out

    def projects(self) -> list[dict[str, Any]]:
        with self._repo() as repo:
            return self._projects_payload(repo)

    def project(self, project_id: str) -> dict[str, Any]:
        with self._repo() as repo:
            for item in self._projects_payload(repo):
                if item["id"] == project_id:
                    return item
        raise EntityNotFound("project not found")
