from __future__ import annotations

import hashlib
import json
from uuid import uuid4

from student_execution_os.domain.errors import (
    AuthorizationDenied,
    EntityNotFound,
    IdempotencyConflict,
    ValidationError,
    VersionConflict,
)
from student_execution_os.domain.model import ActorCategory, LifecycleStatus, Obligation
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository, _iso

from .model import (
    ActionIntent,
    ActionIntentStatus,
    ActionRequest,
    ActionResult,
    AgentCommand,
    AuthenticatedPrincipal,
    IntentStrength,
)


def _fingerprint(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class SQLiteActionGateway:
    """Authenticated LLM action boundary.

    The caller supplies an already authenticated principal. LLM/tool payloads may
    reference only a server-minted intent id; they cannot self-assert actor or scope.
    """

    def __init__(self, canonical: SQLiteCanonicalRepository) -> None:
        self.canonical = canonical
        self.connection = canonical.connection
        self.clock = canonical.clock

    @staticmethod
    def _validate_principal(principal: AuthenticatedPrincipal) -> None:
        if not all(
            (
                principal.account_id,
                principal.principal_id,
                principal.client_id,
            )
        ):
            raise ValidationError(
                "authenticated principal requires account/principal/client identity"
            )

    def mint_intent(
        self,
        *,
        principal: AuthenticatedPrincipal,
        command: AgentCommand,
        target_entity_id: str,
        intent_strength: IntentStrength,
        expected_version: int,
        intent_id: str | None = None,
    ) -> ActionIntent:
        self._validate_principal(principal)
        if command is not AgentCommand.CANCEL_OBLIGATION:
            raise ValidationError("unsupported action command")
        current = self.canonical.get_obligation(
            principal.account_id,
            target_entity_id,
        )
        if current.version != expected_version:
            raise VersionConflict(
                f"expected version {expected_version}, current {current.version}"
            )
        now = self.clock.now()
        explicit = intent_strength is IntentStrength.EXPLICIT_SCOPED
        resolved_id = intent_id or str(uuid4())
        with self.canonical._tx() as conn:
            conn.execute(
                "INSERT INTO action_intents("
                "id,account_id,principal_id,client_id,command,target_entity_id,"
                "intent_strength,expected_version,requires_confirmation,confirmed_at,"
                "status,created_at,consumed_at"
                ") VALUES (?,?,?,?,?,?,?,?,?,?, 'PENDING', ?, NULL)",
                (
                    resolved_id,
                    principal.account_id,
                    principal.principal_id,
                    principal.client_id,
                    command.value,
                    target_entity_id,
                    intent_strength.value,
                    expected_version,
                    0 if explicit else 1,
                    _iso(now) if explicit else None,
                    _iso(now),
                ),
            )
            conn.execute(
                "INSERT INTO action_intent_history("
                "account_id,intent_id,principal_id,action,recorded_at,details_json"
                ") VALUES (?,?,?,?,?,?)",
                (
                    principal.account_id,
                    resolved_id,
                    principal.principal_id,
                    "MINTED",
                    _iso(now),
                    json.dumps(
                        {
                            "command": command.value,
                            "target_entity_id": target_entity_id,
                            "intent_strength": intent_strength.value,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                ),
            )
        return self.get_intent(principal=principal, intent_id=resolved_id)

    def get_intent(
        self,
        *,
        principal: AuthenticatedPrincipal,
        intent_id: str,
    ) -> ActionIntent:
        self._validate_principal(principal)
        row = self.connection.execute(
            "SELECT * FROM action_intents "
            "WHERE account_id=? AND principal_id=? AND client_id=? AND id=?",
            (
                principal.account_id,
                principal.principal_id,
                principal.client_id,
                intent_id,
            ),
        ).fetchone()
        if row is None:
            raise EntityNotFound("action intent not found")
        return self._intent_from_row(row)

    def confirm_intent(
        self,
        *,
        principal: AuthenticatedPrincipal,
        intent_id: str,
    ) -> ActionIntent:
        intent = self.get_intent(principal=principal, intent_id=intent_id)
        if intent.status is not ActionIntentStatus.PENDING:
            raise AuthorizationDenied("only a pending intent can be confirmed")
        with self.canonical._tx() as conn:
            now = self.clock.now()
            updated = conn.execute(
                "UPDATE action_intents SET confirmed_at=?,requires_confirmation=0 "
                "WHERE account_id=? AND principal_id=? AND client_id=? AND id=? "
                "AND status='PENDING'",
                (
                    _iso(now),
                    principal.account_id,
                    principal.principal_id,
                    principal.client_id,
                    intent_id,
                ),
            )
            if updated.rowcount != 1:
                raise AuthorizationDenied("action intent changed before confirmation")
            conn.execute(
                "INSERT INTO action_intent_history("
                "account_id,intent_id,principal_id,action,recorded_at,details_json"
                ") VALUES (?,?,?,?,?,?)",
                (
                    principal.account_id,
                    intent_id,
                    principal.principal_id,
                    "CONFIRMED",
                    _iso(now),
                    "{}",
                ),
            )
        return self.get_intent(principal=principal, intent_id=intent_id)

    def read_obligation(
        self,
        *,
        principal: AuthenticatedPrincipal,
        obligation_id: str,
    ) -> Obligation:
        self._validate_principal(principal)
        return self.canonical.get_obligation(
            principal.account_id,
            obligation_id,
        )

    def execute_cancel(
        self,
        *,
        principal: AuthenticatedPrincipal,
        request: ActionRequest,
    ) -> ActionResult:
        self._validate_principal(principal)
        if not request.intent_id or not request.idempotency_key:
            raise ValidationError("intent_id and idempotency_key are required")
        intent = self.get_intent(
            principal=principal,
            intent_id=request.intent_id,
        )
        if intent.command is not AgentCommand.CANCEL_OBLIGATION:
            raise AuthorizationDenied("intent is not scoped to cancellation")
        if request.expected_version != intent.expected_version:
            raise AuthorizationDenied(
                "action request version is not bound to the server intent"
            )

        payload = {
            "intent_id": intent.id,
            "command": intent.command.value,
            "target_entity_id": intent.target_entity_id,
            "expected_version": request.expected_version,
        }
        digest = _fingerprint(payload)
        replay = self._load_idempotency(
            principal=principal,
            command=intent.command,
            idempotency_key=request.idempotency_key,
        )
        if replay is not None:
            return self._replay_result(
                replay,
                expected_fingerprint=digest,
            )

        self._require_authorized(intent)
        if request.dry_run:
            current = self.canonical.get_obligation(
                principal.account_id,
                intent.target_entity_id,
            )
            if current.version != request.expected_version:
                raise VersionConflict(
                    f"expected version {request.expected_version}, current {current.version}"
                )
            return ActionResult(
                intent_id=intent.id,
                command=intent.command,
                target_entity_id=intent.target_entity_id,
                lifecycle_status=LifecycleStatus.CANCELLED.value,
                entity_version=current.version + 1,
                replayed=False,
                dry_run=True,
            )

        with self.canonical._tx() as conn:
            existing = conn.execute(
                "SELECT request_fingerprint,result_json FROM action_idempotency_records "
                "WHERE account_id=? AND principal_id=? AND client_id=? "
                "AND command_family=? AND idempotency_key=?",
                (
                    principal.account_id,
                    principal.principal_id,
                    principal.client_id,
                    intent.command.value,
                    request.idempotency_key,
                ),
            ).fetchone()
            if existing is not None:
                return self._replay_result(
                    existing,
                    expected_fingerprint=digest,
                )

            row = conn.execute(
                "SELECT * FROM action_intents "
                "WHERE account_id=? AND principal_id=? AND client_id=? AND id=?",
                (
                    principal.account_id,
                    principal.principal_id,
                    principal.client_id,
                    intent.id,
                ),
            ).fetchone()
            if row is None:
                raise EntityNotFound("action intent not found")
            current_intent = self._intent_from_row(row)
            self._require_authorized(current_intent)
            if current_intent.status is not ActionIntentStatus.PENDING:
                raise AuthorizationDenied("action intent is no longer pending")

            result = self.canonical._transition_obligation_in_tx(
                conn,
                account_id=principal.account_id,
                obligation_id=current_intent.target_entity_id,
                expected_version=request.expected_version,
                actor=ActorCategory.USER_VIA_LLM,
                action="CANCEL",
                audit_payload={"intent_id": current_intent.id},
            )
            now = self.clock.now()
            updated = conn.execute(
                "UPDATE action_intents SET status='CONSUMED',consumed_at=? "
                "WHERE account_id=? AND principal_id=? AND client_id=? AND id=? "
                "AND status='PENDING'",
                (
                    _iso(now),
                    principal.account_id,
                    principal.principal_id,
                    principal.client_id,
                    current_intent.id,
                ),
            )
            if updated.rowcount != 1:
                raise AuthorizationDenied("action intent changed before commit")
            conn.execute(
                "INSERT INTO action_intent_history("
                "account_id,intent_id,principal_id,action,recorded_at,details_json"
                ") VALUES (?,?,?,?,?,?)",
                (
                    principal.account_id,
                    current_intent.id,
                    principal.principal_id,
                    "CONSUMED",
                    _iso(now),
                    json.dumps(
                        {"result_version": result.version},
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                ),
            )

            result_payload = {
                "intent_id": current_intent.id,
                "command": current_intent.command.value,
                "target_entity_id": current_intent.target_entity_id,
                "lifecycle_status": result.lifecycle_status.value,
                "entity_version": result.version,
            }
            conn.execute(
                "INSERT INTO action_idempotency_records("
                "account_id,principal_id,client_id,command_family,idempotency_key,"
                "request_fingerprint,intent_id,result_entity_id,result_version,"
                "result_json,created_at"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    principal.account_id,
                    principal.principal_id,
                    principal.client_id,
                    current_intent.command.value,
                    request.idempotency_key,
                    digest,
                    current_intent.id,
                    current_intent.target_entity_id,
                    result.version,
                    json.dumps(
                        result_payload,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    _iso(now),
                ),
            )

        return ActionResult(
            intent_id=current_intent.id,
            command=current_intent.command,
            target_entity_id=current_intent.target_entity_id,
            lifecycle_status=result.lifecycle_status.value,
            entity_version=result.version,
            replayed=False,
            dry_run=False,
        )

    def _load_idempotency(
        self,
        *,
        principal: AuthenticatedPrincipal,
        command: AgentCommand,
        idempotency_key: str,
    ):
        return self.connection.execute(
            "SELECT request_fingerprint,result_json FROM action_idempotency_records "
            "WHERE account_id=? AND principal_id=? AND client_id=? "
            "AND command_family=? AND idempotency_key=?",
            (
                principal.account_id,
                principal.principal_id,
                principal.client_id,
                command.value,
                idempotency_key,
            ),
        ).fetchone()

    @staticmethod
    def _require_authorized(intent: ActionIntent) -> None:
        explicit = intent.intent_strength is IntentStrength.EXPLICIT_SCOPED
        confirmed = intent.confirmed_at is not None
        if not explicit and not confirmed:
            raise AuthorizationDenied(
                "destructive action requires explicit authenticated confirmation"
            )
        if intent.status is ActionIntentStatus.REJECTED:
            raise AuthorizationDenied("action intent was rejected")

    @staticmethod
    def _replay_result(row, *, expected_fingerprint: str) -> ActionResult:
        if row["request_fingerprint"] != expected_fingerprint:
            raise IdempotencyConflict(
                "idempotency key reused with a different semantic request"
            )
        payload = json.loads(row["result_json"])
        return ActionResult(
            intent_id=payload["intent_id"],
            command=AgentCommand(payload["command"]),
            target_entity_id=payload["target_entity_id"],
            lifecycle_status=payload["lifecycle_status"],
            entity_version=int(payload["entity_version"]),
            replayed=True,
            dry_run=False,
        )

    @staticmethod
    def _intent_from_row(row) -> ActionIntent:
        return ActionIntent(
            id=row["id"],
            account_id=row["account_id"],
            principal_id=row["principal_id"],
            client_id=row["client_id"],
            command=AgentCommand(row["command"]),
            target_entity_id=row["target_entity_id"],
            intent_strength=IntentStrength(row["intent_strength"]),
            expected_version=int(row["expected_version"]),
            requires_confirmation=bool(row["requires_confirmation"]),
            confirmed_at=row["confirmed_at"],
            status=ActionIntentStatus(row["status"]),
            created_at=row["created_at"],
            consumed_at=row["consumed_at"],
        )