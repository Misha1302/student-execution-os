"""Account data rights, diagnostics, evidence and beta feedback."""
from __future__ import annotations

from typing import Any
from uuid import uuid4

from student_execution_os import __version__
from student_execution_os.agent import LlmCredentialStore
from student_execution_os.reliability import SQLiteDataLifecycle
from student_execution_os.reminders import provider_from_environment
from student_execution_os.planning import SQLitePlanStore
from student_execution_os.reconciliation import SQLiteReconciliationRepository

from .common import _jsonify

from .base import ApplicationService


class AccountService(ApplicationService):
    """Account data rights, diagnostics, evidence and beta feedback."""

    def evidence(self) -> dict[str, Any]:
        with self._repo() as repo:
            recon = SQLiteReconciliationRepository(repo)
            source_rows = repo.connection.execute(
                "SELECT id,kind,policy_context_json,created_at FROM source_systems WHERE account_id=? ORDER BY id",
                (self.account_id,),
            ).fetchall()
            sources = []
            for row in source_rows:
                status = repo.connection.execute(
                    "SELECT status,recorded_at FROM source_status_history WHERE account_id=? AND source_system_id=? ORDER BY id DESC LIMIT 1",
                    (self.account_id, row["id"]),
                ).fetchone()
                connector = repo.connection.execute(
                    "SELECT id,provider,scope,health_status,last_successful_complete_sync_at,latest_failure_reason,connector_version,version "
                    "FROM connector_states WHERE account_id=? AND source_system_id=? ORDER BY id LIMIT 1",
                    (self.account_id, row["id"]),
                ).fetchone()
                sources.append({
                    "id": row["id"],
                    "kind": row["kind"],
                    "created_at": row["created_at"],
                    "availability": None if status is None else status["status"],
                    "availability_recorded_at": None if status is None else status["recorded_at"],
                    "connector": None if connector is None else dict(connector),
                })
            observations = repo.connection.execute(
                "SELECT o.id,o.binding_id,o.field_path,o.value_type,o.value_json,o.extraction_certainty,o.observed_at,o.extractor_id,"
                "sr.source_system_id,sr.external_entity_id,sr.source_revision "
                "FROM observations o JOIN source_records sr ON sr.id=o.source_record_id AND sr.account_id=o.account_id "
                "WHERE o.account_id=? ORDER BY o.observed_at DESC,o.id LIMIT 200",
                (self.account_id,),
            ).fetchall()
            conflicts = recon.list_conflicts(self.account_id)
            effective_rows = repo.connection.execute(
                "SELECT entity_ref FROM effective_fields WHERE account_id=? ORDER BY entity_ref",
                (self.account_id,),
            ).fetchall()
            effective = []
            for row in effective_rows:
                item = recon.get_effective_cutoff(self.account_id, row["entity_ref"])
                if item is not None:
                    effective.append(_jsonify(item))
            override_rows = repo.connection.execute(
                "SELECT id,entity_ref,field_path,value_type,value_json,status,reason,actor_category,created_at,version "
                "FROM user_overrides WHERE account_id=? ORDER BY created_at DESC,id",
                (self.account_id,),
            ).fetchall()
            return {
                "sources": sources,
                "observations": [dict(r) for r in observations],
                "conflicts": _jsonify(conflicts),
                "effective_fields": effective,
                "overrides": [dict(r) for r in override_rows],
            }

    def beta_feedback(self, payload: dict[str, Any]) -> dict[str, Any]:
        message = str(payload.get("message") or "").strip()
        if not message or len(message) > 5000:
            raise ValueError("feedback message is required and must be at most 5000 characters")
        allowed = {"client_version", "platform", "app_version", "timestamp"}
        context = payload.get("technical_context") or {}
        if not isinstance(context, dict) or set(context) - allowed:
            raise ValueError("technical_context contains unsupported fields")
        import json
        feedback_id = str(uuid4())
        with self._repo() as repo:
            revision = repo.get_server_revision(self.account_id)
            repo.connection.execute(
                "INSERT INTO beta_feedback(id,account_id,message,technical_context_json,server_revision,created_at) VALUES (?,?,?,?,?,?)",
                (feedback_id, self.account_id, message, json.dumps(context, sort_keys=True), revision, _jsonify(self._now())),
            )
            repo.connection.commit()
        return {"id": feedback_id, "status": "RECEIVED"}

    def account_export(self) -> dict[str, Any]:
        return SQLiteDataLifecycle(self.database, now=self._now).export_account(self.account_id).to_dict()

    def account_deletion_policy(self) -> dict[str, Any]:
        with self._repo() as repo:
            revision = repo.get_server_revision(self.account_id)
        policy = SQLiteDataLifecycle(self.database, now=self._now).deletion_policy()
        return {
            "account_id": self.account_id,
            "server_revision": revision,
            **policy,
        }

    def delete_account(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = SQLiteDataLifecycle(self.database, now=self._now).delete_account(
            self.account_id,
            expected_server_revision=int(payload["expected_server_revision"]),
            confirm_account_id=str(payload.get("confirm_account_id", "")),
        )
        return result.to_dict()

    def diagnostics(self) -> dict[str, Any]:
        with self._repo() as repo:
            store = SQLitePlanStore(repo)
            latest = store.get_latest(self.account_id)
            worker = self._worker_status(repo)
            if worker["state"] == "RUNNING":
                fcm = "CONFIGURED" if worker["push_configured"] else "UNCONFIGURED"
            else:
                fcm = "CONFIGURED" if provider_from_environment().configured else "UNCONFIGURED"
            return {
                "service": "student-execution-os",
                "version": __version__,
                "schema_version": repo.schema_version(),
                "server_revision": repo.get_server_revision(self.account_id),
                "account_binding": self.binding,
                "principal_binding": self.binding,
                "latest_plan": None if latest is None else {
                    "id": latest.id,
                    "plan_revision": latest.plan_revision,
                    "input_hash": latest.input_hash,
                    "feasibility_status": latest.feasibility_status.value,
                    "generated_at": _jsonify(latest.generated_at),
                },
                "connector_health": self._source_health(repo),
                "recurring_template_count": repo.connection.execute(
                    "SELECT count(*) FROM recurring_templates WHERE account_id=?", (self.account_id,)
                ).fetchone()[0],
                "reminder_delivery_state": [
                    dict(row) for row in repo.connection.execute(
                        "SELECT delivery_state AS state,count(*) AS count FROM reminder_messages "
                        "WHERE account_id=? GROUP BY delivery_state ORDER BY delivery_state",
                        (self.account_id,),
                    ).fetchall()
                ],
                "reminder_worker": worker,
                "external_capabilities": {
                    # Push credentials live with the worker process, so its heartbeat
                    # is the source of truth; the API's own env is only a fallback.
                    "fcm": fcm,
                    # Which source serves this account's Assistant; never the key itself.
                    "llm": LlmCredentialStore(repo).resolve(self.account_id).source.value,
                    "routing": "UNCONFIGURED",
                    "oauth": "UNCONFIGURED",
                },
            }
