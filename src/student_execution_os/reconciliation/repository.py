from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from student_execution_os.domain.errors import EntityNotFound, ValidationError
from student_execution_os.domain.model import (
    ActorCategory,
    CutoffBoundary,
    CutoffState,
    HardCutoff,
    Importance,
    ObligationCategory,
    Task,
    TemporalPrecision,
)
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository, _dt, _iso
from student_execution_os.reconciliation.model import (
    BindingState,
    ConflictProjection,
    EffectiveField,
    EffectiveFieldState,
    ExtractionCertainty,
    FieldPolicy,
    Observation,
    ObservationValueType,
    OverrideStatus,
    SourceAvailability,
    SourceBinding,
    SourceRecord,
    SourceSystem,
    UserOverride,
)


_FIELD_ACTUAL_CUTOFF = "actual_cutoff"


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _payload_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()


def _encode_value(value_type: ObservationValueType, value: Any) -> Any:
    if value_type is ObservationValueType.HARD_CUTOFF:
        if isinstance(value, HardCutoff):
            return {
                "state": value.state.value,
                "at": value.at.isoformat() if value.at is not None else None,
                "boundary": value.boundary.value if value.boundary is not None else None,
                "precision": value.precision.value if value.precision is not None else None,
            }
        if isinstance(value, dict):
            return dict(value)
        raise ValidationError("HARD_CUTOFF observation requires HardCutoff or typed mapping")
    if value_type is ObservationValueType.BOOLEAN:
        if not isinstance(value, bool):
            raise ValidationError("BOOLEAN observation requires bool")
        return value
    if value_type is ObservationValueType.STRING:
        if not isinstance(value, str):
            raise ValidationError("STRING observation requires str")
        return value
    if value_type in (ObservationValueType.ABSENT, ObservationValueType.SOURCE_REMOVED):
        if value not in (None, {}):
            raise ValidationError(f"{value_type.value} observation cannot carry a scalar value")
        return None
    raise ValidationError(f"unsupported observation value type {value_type}")


def _decode_cutoff(payload: Any) -> HardCutoff:
    if not isinstance(payload, dict):
        raise ValidationError("cutoff payload must be an object")
    state = CutoffState(payload["state"])
    at = _dt(payload.get("at"))
    boundary = CutoffBoundary(payload["boundary"]) if payload.get("boundary") else None
    precision = TemporalPrecision(payload["precision"]) if payload.get("precision") else None
    return HardCutoff(state=state, at=at, boundary=boundary, precision=precision)


def _semantic_key(value_type: ObservationValueType, value_payload: Any) -> str:
    return f"{value_type.value}:{_json(value_payload)}"


def _cutoff_past(cutoff: HardCutoff, now: datetime) -> bool:
    if cutoff.state is not CutoffState.KNOWN or cutoff.at is None:
        return False
    if cutoff.boundary is CutoffBoundary.EXCLUSIVE:
        return now >= cutoff.at
    return now > cutoff.at


class SQLiteReconciliationRepository:
    """Pass-4 evidence and reconciliation adapter.

    Evidence rows are immutable. Binding/override/conflict workflow state remains
    auditable through append-only history. Only this boundary writes effective_fields.
    """

    def __init__(self, canonical: SQLiteCanonicalRepository) -> None:
        self.canonical = canonical
        self.connection = canonical.connection
        self.clock = canonical.clock

    def _audit(
        self,
        conn,
        *,
        account_id: str,
        action: str,
        actor: ActorCategory,
        entity_ref: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        conn.execute(
            "INSERT INTO reconciliation_audit(account_id,action,actor_category,entity_ref,committed_at,payload_json) "
            "VALUES (?,?,?,?,?,?)",
            (account_id, action, actor.value, entity_ref, _iso(self.clock.now()), _json(payload or {})),
        )

    def list_reconciliation_audit(self, account_id: str) -> list[dict[str, Any]]:
        self.canonical._require_account(account_id)
        rows = self.connection.execute(
            "SELECT * FROM reconciliation_audit WHERE account_id=? ORDER BY id", (account_id,)
        ).fetchall()
        return [
            {
                "action": row["action"],
                "actor_category": row["actor_category"],
                "entity_ref": row["entity_ref"],
                "committed_at": row["committed_at"],
                "payload": json.loads(row["payload_json"]),
            }
            for row in rows
        ]

    def create_source_system(
        self,
        *,
        account_id: str,
        kind: str,
        actor: ActorCategory,
        policy_context: dict[str, Any] | None = None,
        source_system_id: str | None = None,
    ) -> SourceSystem:
        self.canonical._require_account(account_id)
        source = SourceSystem(
            id=source_system_id or str(uuid4()),
            account_id=account_id,
            kind=kind,
            policy_context=dict(policy_context or {}),
            created_at=self.clock.now(),
        )
        with self.canonical._tx() as conn:
            conn.execute(
                "INSERT INTO source_systems(id,account_id,kind,policy_context_json,created_at) VALUES (?,?,?,?,?)",
                (source.id, account_id, source.kind, _json(dict(source.policy_context)), _iso(source.created_at)),
            )
            conn.execute(
                "INSERT INTO source_status_history(account_id,source_system_id,status,recorded_at,actor_category) VALUES (?,?,?,?,?)",
                (account_id, source.id, SourceAvailability.ACTIVE.value, _iso(self.clock.now()), actor.value),
            )
            self._audit(
                conn,
                account_id=account_id,
                action="CREATE_SOURCE_SYSTEM",
                actor=actor,
                entity_ref=source.id,
                payload={"kind": kind},
            )
        return source

    def get_source_system(self, account_id: str, source_system_id: str) -> SourceSystem:
        row = self.connection.execute(
            "SELECT * FROM source_systems WHERE account_id=? AND id=?", (account_id, source_system_id)
        ).fetchone()
        if row is None:
            raise EntityNotFound("source system not found")
        return SourceSystem(
            id=row["id"],
            account_id=row["account_id"],
            kind=row["kind"],
            policy_context=json.loads(row["policy_context_json"]),
            created_at=_dt(row["created_at"]),
        )

    def _mark_source_availability_in_tx(
        self,
        conn,
        *,
        account_id: str,
        source_system_id: str,
        status: SourceAvailability,
        actor: ActorCategory,
    ) -> None:
        if conn.execute(
            "SELECT 1 FROM source_systems WHERE account_id=? AND id=?",
            (account_id, source_system_id),
        ).fetchone() is None:
            raise EntityNotFound("source system not found")
        conn.execute(
            "INSERT INTO source_status_history(account_id,source_system_id,status,recorded_at,actor_category) "
            "VALUES (?,?,?,?,?)",
            (
                account_id,
                source_system_id,
                status.value,
                _iso(self.clock.now()),
                actor.value,
            ),
        )
        self._audit(
            conn,
            account_id=account_id,
            action="SOURCE_AVAILABILITY",
            actor=actor,
            entity_ref=source_system_id,
            payload={"status": status.value},
        )

    def mark_source_availability(
        self,
        *,
        account_id: str,
        source_system_id: str,
        status: SourceAvailability,
        actor: ActorCategory,
    ) -> None:
        self.get_source_system(account_id, source_system_id)
        with self.canonical._tx() as conn:
            self._mark_source_availability_in_tx(
                conn,
                account_id=account_id,
                source_system_id=source_system_id,
                status=status,
                actor=actor,
            )

    def current_source_availability(self, account_id: str, source_system_id: str) -> SourceAvailability:
        self.get_source_system(account_id, source_system_id)
        row = self.connection.execute(
            "SELECT status FROM source_status_history WHERE account_id=? AND source_system_id=? ORDER BY id DESC LIMIT 1",
            (account_id, source_system_id),
        ).fetchone()
        return SourceAvailability(row["status"])

    def add_source_record(
        self,
        *,
        account_id: str,
        source_system_id: str,
        actor: ActorCategory,
        external_entity_id: str | None = None,
        source_revision: str | None = None,
        revision_order: int | None = None,
        observed_at: datetime | None = None,
        content_hash: str | None = None,
        source_uri: str | None = None,
        raw_payload_ref: str | None = None,
        metadata: dict[str, Any] | None = None,
        source_record_id: str | None = None,
    ) -> SourceRecord:
        self.get_source_system(account_id, source_system_id)
        observed_at = observed_at or self.clock.now()
        record = SourceRecord(
            id=source_record_id or str(uuid4()),
            account_id=account_id,
            source_system_id=source_system_id,
            external_entity_id=external_entity_id,
            source_revision=source_revision,
            revision_order=revision_order,
            observed_at=observed_at,
            content_hash=content_hash,
            source_uri=source_uri,
            raw_payload_ref=raw_payload_ref,
            metadata=dict(metadata or {}),
        )
        with self.canonical._tx() as conn:
            conn.execute(
                "INSERT INTO source_records(id,account_id,source_system_id,external_entity_id,source_revision,revision_order,observed_at,content_hash,source_uri,raw_payload_ref,metadata_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record.id,
                    account_id,
                    source_system_id,
                    external_entity_id,
                    source_revision,
                    revision_order,
                    _iso(record.observed_at),
                    content_hash,
                    source_uri,
                    raw_payload_ref,
                    _json(dict(record.metadata)),
                ),
            )
            self._audit(
                conn,
                account_id=account_id,
                action="INGEST_SOURCE_RECORD",
                actor=actor,
                entity_ref=record.id,
                payload={"source_system_id": source_system_id, "external_entity_id": external_entity_id},
            )
        return record

    def get_source_record(self, account_id: str, source_record_id: str) -> SourceRecord:
        row = self.connection.execute(
            "SELECT * FROM source_records WHERE account_id=? AND id=?", (account_id, source_record_id)
        ).fetchone()
        if row is None:
            raise EntityNotFound("source record not found")
        return SourceRecord(
            id=row["id"],
            account_id=row["account_id"],
            source_system_id=row["source_system_id"],
            external_entity_id=row["external_entity_id"],
            source_revision=row["source_revision"],
            revision_order=row["revision_order"],
            observed_at=_dt(row["observed_at"]),
            content_hash=row["content_hash"],
            source_uri=row["source_uri"],
            raw_payload_ref=row["raw_payload_ref"],
            metadata=json.loads(row["metadata_json"]),
        )

    def add_observation(
        self,
        *,
        account_id: str,
        source_record_id: str,
        field_path: str,
        value_type: ObservationValueType,
        value: Any,
        extraction_certainty: ExtractionCertainty,
        extractor_id: str,
        actor: ActorCategory,
        observed_at: datetime | None = None,
        binding_id: str | None = None,
        observation_id: str | None = None,
    ) -> Observation:
        source_record = self.get_source_record(account_id, source_record_id)
        if binding_id is not None:
            binding = self.get_binding(account_id, binding_id)
            if (
                binding.source_system_id != source_record.source_system_id
                or binding.external_entity_id != source_record.external_entity_id
            ):
                raise ValidationError("observation binding must match its source-native entity")
        if not extractor_id:
            raise ValidationError("extractor_id is required and is distinct from source/actor")
        encoded = _encode_value(value_type, value)
        observed_at = observed_at or source_record.observed_at
        observation = Observation(
            id=observation_id or str(uuid4()),
            account_id=account_id,
            source_record_id=source_record_id,
            binding_id=binding_id,
            field_path=field_path,
            value_type=value_type,
            value=encoded,
            extraction_certainty=extraction_certainty,
            observed_at=observed_at,
            extractor_id=extractor_id,
        )
        with self.canonical._tx() as conn:
            conn.execute(
                "INSERT INTO observations(id,account_id,source_record_id,binding_id,field_path,value_type,value_json,extraction_certainty,observed_at,extractor_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    observation.id,
                    account_id,
                    source_record_id,
                    binding_id,
                    field_path,
                    value_type.value,
                    _json(encoded),
                    extraction_certainty.value,
                    _iso(observed_at),
                    extractor_id,
                ),
            )
            self._audit(
                conn,
                account_id=account_id,
                action="INGEST_OBSERVATION",
                actor=actor,
                entity_ref=observation.id,
                payload={
                    "source_record_id": source_record_id,
                    "field_path": field_path,
                    "extractor_id": extractor_id,
                },
            )
            if field_path == _FIELD_ACTUAL_CUTOFF and source_record.external_entity_id is not None:
                rows = conn.execute(
                    "SELECT local_entity_id FROM source_bindings WHERE account_id=? AND source_system_id=? "
                    "AND external_entity_id=? AND state='ACTIVE'",
                    (account_id, source_record.source_system_id, source_record.external_entity_id),
                ).fetchall()
                for row in rows:
                    self._reconcile_cutoff_in_tx(
                        conn,
                        account_id,
                        row["local_entity_id"],
                        actor=ActorCategory.RECONCILER,
                    )
        return observation

    def observation_exists(self, account_id: str, observation_id: str) -> bool:
        self.canonical._require_account(account_id)
        return self.connection.execute(
            "SELECT 1 FROM observations WHERE account_id=? AND id=?",
            (account_id, observation_id),
        ).fetchone() is not None

    def get_observation(self, account_id: str, observation_id: str) -> Observation:
        row = self.connection.execute(
            "SELECT * FROM observations WHERE account_id=? AND id=?",
            (account_id, observation_id),
        ).fetchone()
        if row is None:
            raise EntityNotFound("observation not found")
        return Observation(
            id=row["id"],
            account_id=row["account_id"],
            source_record_id=row["source_record_id"],
            binding_id=row["binding_id"],
            field_path=row["field_path"],
            value_type=ObservationValueType(row["value_type"]),
            value=json.loads(row["value_json"]),
            extraction_certainty=ExtractionCertainty(row["extraction_certainty"]),
            observed_at=_dt(row["observed_at"]),
            extractor_id=row["extractor_id"],
        )

    def list_observations(
        self,
        account_id: str,
        *,
        source_record_id: str | None = None,
    ) -> list[Observation]:
        self.canonical._require_account(account_id)
        sql = "SELECT * FROM observations WHERE account_id=?"
        args: list[Any] = [account_id]
        if source_record_id is not None:
            sql += " AND source_record_id=?"
            args.append(source_record_id)
        sql += " ORDER BY observed_at,id"
        rows = self.connection.execute(sql, tuple(args)).fetchall()
        return [
            Observation(
                id=row["id"],
                account_id=row["account_id"],
                source_record_id=row["source_record_id"],
                binding_id=row["binding_id"],
                field_path=row["field_path"],
                value_type=ObservationValueType(row["value_type"]),
                value=json.loads(row["value_json"]),
                extraction_certainty=ExtractionCertainty(row["extraction_certainty"]),
                observed_at=_dt(row["observed_at"]),
                extractor_id=row["extractor_id"],
            )
            for row in rows
        ]

    def bind_source_entity(
        self,
        *,
        account_id: str,
        source_system_id: str,
        external_entity_id: str,
        local_entity_id: str,
        match_decision_id: str,
        actor: ActorCategory,
        binding_id: str | None = None,
    ) -> SourceBinding:
        self.get_source_system(account_id, source_system_id)
        if self.connection.execute(
            "SELECT 1 FROM obligations WHERE account_id=? AND id=?",
            (account_id, local_entity_id),
        ).fetchone() is None:
            raise EntityNotFound("local obligation not found")
        now = self.clock.now()
        new_id = binding_id or str(uuid4())
        old_entities: list[str] = []
        with self.canonical._tx() as conn:
            active = conn.execute(
                "SELECT id,local_entity_id FROM source_bindings WHERE account_id=? AND source_system_id=? "
                "AND external_entity_id=? AND state='ACTIVE'",
                (account_id, source_system_id, external_entity_id),
            ).fetchall()
            for row in active:
                if row["local_entity_id"] == local_entity_id:
                    return self.get_binding(account_id, row["id"])
                old_entities.append(row["local_entity_id"])
                conn.execute(
                    "UPDATE source_bindings SET state='DETACHED',updated_at=?,version=version+1 WHERE id=?",
                    (_iso(now), row["id"]),
                )
                conn.execute(
                    "INSERT INTO binding_history(binding_id,state,recorded_at,actor_category,reason) VALUES (?,?,?,?,?)",
                    (
                        row["id"],
                        BindingState.DETACHED.value,
                        _iso(now),
                        actor.value,
                        f"rebound-to:{local_entity_id}",
                    ),
                )
            conn.execute(
                "INSERT INTO source_bindings(id,account_id,source_system_id,external_entity_id,local_entity_id,state,match_decision_id,created_at,updated_at,version) "
                "VALUES (?,?,?,?,?,'ACTIVE',?,?,?,1)",
                (
                    new_id,
                    account_id,
                    source_system_id,
                    external_entity_id,
                    local_entity_id,
                    match_decision_id,
                    _iso(now),
                    _iso(now),
                ),
            )
            conn.execute(
                "INSERT INTO binding_history(binding_id,state,recorded_at,actor_category,reason) VALUES (?,?,?,?,?)",
                (new_id, BindingState.ACTIVE.value, _iso(now), actor.value, "bind"),
            )
            self._audit(
                conn,
                account_id=account_id,
                action="BIND_SOURCE_ENTITY",
                actor=actor,
                entity_ref=local_entity_id,
                payload={
                    "source_system_id": source_system_id,
                    "external_entity_id": external_entity_id,
                    "binding_id": new_id,
                },
            )
            for entity_id in [*old_entities, local_entity_id]:
                self._reconcile_cutoff_in_tx(
                    conn,
                    account_id,
                    entity_id,
                    actor=ActorCategory.RECONCILER,
                )
        return self.get_binding(account_id, new_id)

    def get_binding(self, account_id: str, binding_id: str) -> SourceBinding:
        row = self.connection.execute(
            "SELECT * FROM source_bindings WHERE account_id=? AND id=?",
            (account_id, binding_id),
        ).fetchone()
        if row is None:
            raise EntityNotFound("source binding not found")
        return SourceBinding(
            id=row["id"],
            account_id=row["account_id"],
            source_system_id=row["source_system_id"],
            external_entity_id=row["external_entity_id"],
            local_entity_id=row["local_entity_id"],
            state=BindingState(row["state"]),
            match_decision_id=row["match_decision_id"],
            version=int(row["version"]),
        )

    def list_bindings(self, account_id: str) -> list[SourceBinding]:
        self.canonical._require_account(account_id)
        rows = self.connection.execute(
            "SELECT id FROM source_bindings WHERE account_id=? ORDER BY created_at,id",
            (account_id,),
        ).fetchall()
        return [self.get_binding(account_id, row["id"]) for row in rows]

    def create_field_policy(
        self,
        *,
        account_id: str,
        field_path: str,
        version: str,
        min_certainty: ExtractionCertainty,
        source_authority: dict[str, int],
        actor: ActorCategory,
        allow_user_override: bool = True,
        override_is_hard_planning_input: bool = True,
        conflict_projection: ConflictProjection = ConflictProjection.NONE,
        freshness_rule: str = "REVISION_THEN_OBSERVED_AT",
    ) -> FieldPolicy:
        self.canonical._require_account(account_id)
        policy = FieldPolicy(
            account_id=account_id,
            field_path=field_path,
            version=version,
            min_certainty=min_certainty,
            source_authority=dict(source_authority),
            freshness_rule=freshness_rule,
            allow_user_override=allow_user_override,
            override_is_hard_planning_input=override_is_hard_planning_input,
            conflict_projection=conflict_projection,
            created_at=self.clock.now(),
        )
        with self.canonical._tx() as conn:
            conn.execute(
                "INSERT INTO reconciliation_policies(account_id,field_path,version,min_certainty,source_authority_json,freshness_rule,allow_user_override,override_is_hard_planning_input,conflict_projection,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    account_id,
                    field_path,
                    version,
                    min_certainty.value,
                    _json(source_authority),
                    freshness_rule,
                    int(allow_user_override),
                    int(override_is_hard_planning_input),
                    conflict_projection.value,
                    _iso(policy.created_at),
                ),
            )
            self._audit(
                conn,
                account_id=account_id,
                action="CREATE_FIELD_POLICY",
                actor=actor,
                entity_ref=field_path,
                payload={"version": version},
            )
            if field_path == _FIELD_ACTUAL_CUTOFF:
                entities = conn.execute(
                    "SELECT DISTINCT local_entity_id FROM source_bindings WHERE account_id=? "
                    "UNION SELECT DISTINCT entity_ref FROM user_overrides WHERE account_id=? AND field_path=?",
                    (account_id, account_id, field_path),
                ).fetchall()
                for row in entities:
                    self._reconcile_cutoff_in_tx(
                        conn,
                        account_id,
                        row[0],
                        actor=ActorCategory.RECONCILER,
                    )
        return policy

    def current_field_policy(self, account_id: str, field_path: str) -> FieldPolicy | None:
        row = self.connection.execute(
            "SELECT * FROM reconciliation_policies WHERE account_id=? AND field_path=? ORDER BY id DESC LIMIT 1",
            (account_id, field_path),
        ).fetchone()
        if row is None:
            return None
        return FieldPolicy(
            account_id=row["account_id"],
            field_path=row["field_path"],
            version=row["version"],
            min_certainty=ExtractionCertainty(row["min_certainty"]),
            source_authority=json.loads(row["source_authority_json"]),
            freshness_rule=row["freshness_rule"],
            allow_user_override=bool(row["allow_user_override"]),
            override_is_hard_planning_input=bool(row["override_is_hard_planning_input"]),
            conflict_projection=ConflictProjection(row["conflict_projection"]),
            created_at=_dt(row["created_at"]),
        )

    def create_override(
        self,
        *,
        account_id: str,
        entity_ref: str,
        field_path: str,
        value_type: ObservationValueType,
        value: Any,
        actor: ActorCategory,
        reason: str | None = None,
        override_id: str | None = None,
    ) -> UserOverride:
        policy = self.current_field_policy(account_id, field_path)
        if policy is None or not policy.allow_user_override:
            raise ValidationError("active field policy does not permit user override")
        if actor not in (ActorCategory.USER_UI, ActorCategory.USER_VIA_LLM):
            raise ValidationError("user override requires authenticated user-origin actor")
        if self.connection.execute(
            "SELECT 1 FROM obligations WHERE account_id=? AND id=?",
            (account_id, entity_ref),
        ).fetchone() is None:
            raise EntityNotFound("local obligation not found")
        encoded = _encode_value(value_type, value)
        now = self.clock.now()
        new_id = override_id or str(uuid4())
        with self.canonical._tx() as conn:
            active = conn.execute(
                "SELECT id,version FROM user_overrides WHERE account_id=? AND entity_ref=? AND field_path=? AND status='ACTIVE'",
                (account_id, entity_ref, field_path),
            ).fetchall()
            for row in active:
                conn.execute(
                    "UPDATE user_overrides SET status='SUPERSEDED',version=version+1 WHERE id=?",
                    (row["id"],),
                )
                conn.execute(
                    "INSERT INTO override_history(override_id,status,recorded_at,actor_category,reason) VALUES (?,?,?,?,?)",
                    (
                        row["id"],
                        OverrideStatus.SUPERSEDED.value,
                        _iso(now),
                        actor.value,
                        f"superseded-by:{new_id}",
                    ),
                )
            conn.execute(
                "INSERT INTO user_overrides(id,account_id,entity_ref,field_path,value_type,value_json,status,reason,actor_category,created_at,version) "
                "VALUES (?,?,?,?,?,?,'ACTIVE',?,?,?,1)",
                (
                    new_id,
                    account_id,
                    entity_ref,
                    field_path,
                    value_type.value,
                    _json(encoded),
                    reason,
                    actor.value,
                    _iso(now),
                ),
            )
            conn.execute(
                "INSERT INTO override_history(override_id,status,recorded_at,actor_category,reason) VALUES (?,?,?,?,?)",
                (new_id, OverrideStatus.ACTIVE.value, _iso(now), actor.value, reason),
            )
            self._audit(
                conn,
                account_id=account_id,
                action="CREATE_OVERRIDE",
                actor=actor,
                entity_ref=entity_ref,
                payload={"field_path": field_path, "override_id": new_id},
            )
            if field_path == _FIELD_ACTUAL_CUTOFF:
                self._reconcile_cutoff_in_tx(
                    conn,
                    account_id,
                    entity_ref,
                    actor=ActorCategory.RECONCILER,
                )
        return self.get_override(account_id, new_id)

    def revoke_override(
        self,
        *,
        account_id: str,
        override_id: str,
        actor: ActorCategory,
        reason: str | None = None,
    ) -> UserOverride:
        current = self.get_override(account_id, override_id)
        if current.status is not OverrideStatus.ACTIVE:
            raise ValidationError("only active override can be revoked")
        now = self.clock.now()
        with self.canonical._tx() as conn:
            conn.execute(
                "UPDATE user_overrides SET status='REVOKED',version=version+1 WHERE id=?",
                (override_id,),
            )
            conn.execute(
                "INSERT INTO override_history(override_id,status,recorded_at,actor_category,reason) VALUES (?,?,?,?,?)",
                (override_id, OverrideStatus.REVOKED.value, _iso(now), actor.value, reason),
            )
            self._audit(
                conn,
                account_id=account_id,
                action="REVOKE_OVERRIDE",
                actor=actor,
                entity_ref=current.entity_ref,
                payload={"field_path": current.field_path, "override_id": override_id},
            )
            if current.field_path == _FIELD_ACTUAL_CUTOFF:
                self._reconcile_cutoff_in_tx(
                    conn,
                    account_id,
                    current.entity_ref,
                    actor=ActorCategory.RECONCILER,
                )
        return self.get_override(account_id, override_id)

    def get_override(self, account_id: str, override_id: str) -> UserOverride:
        row = self.connection.execute(
            "SELECT * FROM user_overrides WHERE account_id=? AND id=?",
            (account_id, override_id),
        ).fetchone()
        if row is None:
            raise EntityNotFound("override not found")
        return UserOverride(
            id=row["id"],
            account_id=row["account_id"],
            entity_ref=row["entity_ref"],
            field_path=row["field_path"],
            value_type=ObservationValueType(row["value_type"]),
            value=json.loads(row["value_json"]),
            status=OverrideStatus(row["status"]),
            reason=row["reason"],
            actor_category=row["actor_category"],
            created_at=_dt(row["created_at"]),
            version=int(row["version"]),
        )

    def list_overrides(
        self,
        account_id: str,
        entity_ref: str,
        field_path: str,
    ) -> list[UserOverride]:
        rows = self.connection.execute(
            "SELECT id FROM user_overrides WHERE account_id=? AND entity_ref=? AND field_path=? ORDER BY created_at,id",
            (account_id, entity_ref, field_path),
        ).fetchall()
        return [self.get_override(account_id, row["id"]) for row in rows]

    def _current_source_observations(
        self,
        conn,
        account_id: str,
        entity_ref: str,
        field_path: str,
    ):
        rows = conn.execute(
            "SELECT o.*,sr.source_system_id,sr.external_entity_id,sr.source_revision,sr.revision_order,"
            "sr.observed_at AS source_observed_at,ss.policy_context_json "
            "FROM observations o "
            "JOIN source_records sr ON sr.id=o.source_record_id AND sr.account_id=o.account_id "
            "JOIN source_systems ss ON ss.id=sr.source_system_id AND ss.account_id=sr.account_id "
            "JOIN source_bindings b ON b.account_id=sr.account_id AND b.source_system_id=sr.source_system_id "
            "AND b.external_entity_id=sr.external_entity_id AND b.local_entity_id=? AND b.state='ACTIVE' "
            "WHERE o.account_id=? AND o.field_path=? ORDER BY o.id",
            (entity_ref, account_id, field_path),
        ).fetchall()
        grouped: dict[tuple[str, str], list[Any]] = {}
        for row in rows:
            grouped.setdefault(
                (row["source_system_id"], row["external_entity_id"]),
                [],
            ).append(row)
        current = []
        for group in grouped.values():
            def key(row):
                has_revision = row["revision_order"] is not None
                return (
                    1 if has_revision else 0,
                    int(row["revision_order"]) if has_revision else -1,
                    row["source_observed_at"],
                    row["observed_at"],
                    row["id"],
                )
            current.append(max(group, key=key))
        return current

    def _removed_evidence_ids(
        self,
        conn,
        account_id: str,
        entity_ref: str,
    ) -> tuple[str, ...]:
        rows = conn.execute(
            "SELECT DISTINCT o.id FROM observations o "
            "JOIN source_records sr ON sr.id=o.source_record_id AND sr.account_id=o.account_id "
            "JOIN source_bindings b ON b.account_id=sr.account_id AND b.source_system_id=sr.source_system_id "
            "AND b.external_entity_id=sr.external_entity_id AND b.local_entity_id=? AND b.state='SOURCE_REMOVED' "
            "WHERE o.account_id=? AND o.value_type='SOURCE_REMOVED' ORDER BY o.id",
            (entity_ref, account_id),
        ).fetchall()
        return tuple(row["id"] for row in rows)

    def _active_override(
        self,
        conn,
        account_id: str,
        entity_ref: str,
        field_path: str,
    ):
        return conn.execute(
            "SELECT * FROM user_overrides WHERE account_id=? AND entity_ref=? AND field_path=? AND status='ACTIVE' "
            "ORDER BY created_at DESC,id DESC LIMIT 1",
            (account_id, entity_ref, field_path),
        ).fetchone()

    def _compute_cutoff(
        self,
        conn,
        account_id: str,
        entity_ref: str,
    ) -> EffectiveField | None:
        policy = self.current_field_policy(account_id, _FIELD_ACTUAL_CUTOFF)
        rows = self._current_source_observations(
            conn,
            account_id,
            entity_ref,
            _FIELD_ACTUAL_CUTOFF,
        )
        override = self._active_override(
            conn,
            account_id,
            entity_ref,
            _FIELD_ACTUAL_CUTOFF,
        )
        existing = conn.execute(
            "SELECT 1 FROM effective_fields WHERE account_id=? AND entity_ref=? AND field_path=?",
            (account_id, entity_ref, _FIELD_ACTUAL_CUTOFF),
        ).fetchone()
        if policy is None:
            return None
        if not rows and override is None and existing is None:
            return None

        if override is not None and policy.allow_user_override:
            vt = ObservationValueType(override["value_type"])
            payload = json.loads(override["value_json"])
            if vt is ObservationValueType.ABSENT:
                cutoff = HardCutoff.absent()
            elif vt is ObservationValueType.HARD_CUTOFF:
                cutoff = _decode_cutoff(payload)
            else:
                raise ValidationError("actual_cutoff override must be HARD_CUTOFF or ABSENT")
            evidence_ids = tuple(sorted(row["id"] for row in rows))
            return EffectiveField(
                account_id,
                entity_ref,
                _FIELD_ACTUAL_CUTOFF,
                EffectiveFieldState.OVERRIDDEN,
                vt,
                payload,
                evidence_ids,
                policy.version,
                cutoff if policy.override_is_hard_planning_input else None,
                (cutoff,) if cutoff.state is CutoffState.KNOWN else (),
                "ACTIVE_USER_OVERRIDE",
                None,
                override["id"],
            )

        usable = []
        all_current_ids = tuple(sorted(row["id"] for row in rows))
        for row in rows:
            certainty = ExtractionCertainty(row["extraction_certainty"])
            if certainty.rank < policy.min_certainty.rank:
                continue
            vt = ObservationValueType(row["value_type"])
            if vt is ObservationValueType.SOURCE_REMOVED:
                continue
            context = json.loads(row["policy_context_json"])
            authority = policy.authority_for(row["source_system_id"], context)
            if authority is None:
                continue
            usable.append(
                (
                    authority,
                    row,
                    vt,
                    json.loads(row["value_json"]),
                )
            )

        if not usable:
            removed = self._removed_evidence_ids(conn, account_id, entity_ref)
            return EffectiveField(
                account_id,
                entity_ref,
                _FIELD_ACTUAL_CUTOFF,
                EffectiveFieldState.UNKNOWN,
                None,
                None,
                all_current_ids or removed,
                policy.version,
                None,
                (),
                "NO_USABLE_CURRENT_EVIDENCE",
                None,
            )

        top_authority = max(item[0] for item in usable)
        top = [item for item in usable if item[0] == top_authority]
        grouped: dict[str, tuple[ObservationValueType, Any, list[str]]] = {}
        for _, row, vt, payload in top:
            key = _semantic_key(vt, payload)
            if key not in grouped:
                grouped[key] = (vt, payload, [])
            grouped[key][2].append(row["id"])
        top_evidence_ids = tuple(sorted(item[1]["id"] for item in top))

        if len(grouped) == 1:
            vt, payload, _ids = next(iter(grouped.values()))
            if vt is ObservationValueType.ABSENT:
                return EffectiveField(
                    account_id,
                    entity_ref,
                    _FIELD_ACTUAL_CUTOFF,
                    EffectiveFieldState.ABSENT,
                    vt,
                    None,
                    top_evidence_ids,
                    policy.version,
                    HardCutoff.absent(),
                    (),
                    "EXPLICIT_ABSENCE",
                    None,
                )
            if vt is not ObservationValueType.HARD_CUTOFF:
                return EffectiveField(
                    account_id,
                    entity_ref,
                    _FIELD_ACTUAL_CUTOFF,
                    EffectiveFieldState.UNKNOWN,
                    vt,
                    payload,
                    top_evidence_ids,
                    policy.version,
                    None,
                    (),
                    "UNSUPPORTED_VALUE_TYPE",
                    None,
                )
            cutoff = _decode_cutoff(payload)
            if cutoff.state is CutoffState.ABSENT:
                return EffectiveField(
                    account_id,
                    entity_ref,
                    _FIELD_ACTUAL_CUTOFF,
                    EffectiveFieldState.ABSENT,
                    vt,
                    payload,
                    top_evidence_ids,
                    policy.version,
                    HardCutoff.absent(),
                    (),
                    "EXPLICIT_ABSENCE",
                    None,
                )
            if cutoff.state is not CutoffState.KNOWN:
                return EffectiveField(
                    account_id,
                    entity_ref,
                    _FIELD_ACTUAL_CUTOFF,
                    EffectiveFieldState.UNKNOWN,
                    vt,
                    payload,
                    top_evidence_ids,
                    policy.version,
                    None,
                    (),
                    "IMPRECISE_OR_UNKNOWN_CUTOFF",
                    None,
                )
            return EffectiveField(
                account_id,
                entity_ref,
                _FIELD_ACTUAL_CUTOFF,
                EffectiveFieldState.RESOLVED,
                vt,
                payload,
                top_evidence_ids,
                policy.version,
                cutoff,
                (cutoff,),
                "ONE_TOP_AUTHORITY_VALUE",
                None,
            )

        admissible: list[HardCutoff] = []
        for vt, payload, _ids in grouped.values():
            if vt is ObservationValueType.HARD_CUTOFF:
                cutoff = _decode_cutoff(payload)
                if cutoff.state is CutoffState.KNOWN:
                    admissible.append(cutoff)

        projection = None
        if (
            policy.conflict_projection is ConflictProjection.EARLIEST_HARD_CUTOFF
            and admissible
        ):
            projection = min(
                admissible,
                key=lambda c: (
                    c.at,
                    0 if c.boundary is CutoffBoundary.EXCLUSIVE else 1,
                ),
            )
        return EffectiveField(
            account_id,
            entity_ref,
            _FIELD_ACTUAL_CUTOFF,
            EffectiveFieldState.CONFLICT,
            None,
            None,
            top_evidence_ids,
            policy.version,
            projection,
            tuple(admissible),
            (
                "EXACT_CUTOFF_VALUES_CONFLICT"
                if len(admissible) == len(grouped)
                else "TOP_AUTHORITY_VALUES_CONFLICT"
            ),
            None,
        )

    def _effective_signature(self, effective: EffectiveField) -> dict[str, Any]:
        return {
            "state": effective.state.value,
            "value_type": effective.value_type.value if effective.value_type else None,
            "value": effective.value,
            "evidence_ids": list(effective.evidence_ids),
            "policy_version": effective.policy_version,
            "planning_projection": (
                None
                if effective.planning_projection is None
                else _encode_value(
                    ObservationValueType.HARD_CUTOFF,
                    effective.planning_projection,
                )
            ),
            "admissible_cutoffs": [
                _encode_value(ObservationValueType.HARD_CUTOFF, cutoff)
                for cutoff in effective.admissible_cutoffs
            ],
            "reason": effective.reason,
            "override_id": effective.override_id,
        }

    def _manage_conflict_in_tx(
        self,
        conn,
        effective: EffectiveField,
        actor: ActorCategory,
    ) -> str | None:
        row = conn.execute(
            "SELECT * FROM reconciliation_conflicts WHERE account_id=? AND entity_ref=? AND field_path=? AND status='OPEN'",
            (effective.account_id, effective.entity_ref, effective.field_path),
        ).fetchone()
        if effective.state is EffectiveFieldState.CONFLICT:
            evidence_json = _json(list(effective.evidence_ids))
            if (
                row is not None
                and row["evidence_ids_json"] == evidence_json
                and row["policy_version"] == effective.policy_version
            ):
                return row["id"]
            if row is not None:
                conn.execute(
                    "UPDATE reconciliation_conflicts SET status='SUPERSEDED',resolved_at=?,version=version+1 WHERE id=?",
                    (_iso(self.clock.now()), row["id"]),
                )
                conn.execute(
                    "INSERT INTO conflict_history(conflict_id,status,recorded_at,actor_category,resolution_ref) VALUES (?,?,?,?,?)",
                    (
                        row["id"],
                        "SUPERSEDED",
                        _iso(self.clock.now()),
                        actor.value,
                        None,
                    ),
                )
            conflict_id = str(uuid4())
            conn.execute(
                "INSERT INTO reconciliation_conflicts(id,account_id,entity_ref,field_path,evidence_ids_json,policy_version,status,created_at,resolved_at,resolution_ref,version) "
                "VALUES (?,?,?,?,?,?,'OPEN',?,NULL,NULL,1)",
                (
                    conflict_id,
                    effective.account_id,
                    effective.entity_ref,
                    effective.field_path,
                    evidence_json,
                    effective.policy_version,
                    _iso(self.clock.now()),
                ),
            )
            conn.execute(
                "INSERT INTO conflict_history(conflict_id,status,recorded_at,actor_category,resolution_ref) VALUES (?,?,?,?,?)",
                (
                    conflict_id,
                    "OPEN",
                    _iso(self.clock.now()),
                    actor.value,
                    None,
                ),
            )
            return conflict_id

        if row is not None:
            status = (
                "RESOLVED"
                if effective.state
                in (
                    EffectiveFieldState.RESOLVED,
                    EffectiveFieldState.OVERRIDDEN,
                    EffectiveFieldState.ABSENT,
                )
                else "SUPERSEDED"
            )
            resolution_ref = (
                f"override:{effective.override_id}"
                if effective.state is EffectiveFieldState.OVERRIDDEN
                and effective.override_id is not None
                else f"effective:{effective.state.value}"
            )
            conn.execute(
                "UPDATE reconciliation_conflicts SET status=?,resolved_at=?,resolution_ref=?,version=version+1 WHERE id=?",
                (
                    status,
                    _iso(self.clock.now()),
                    resolution_ref,
                    row["id"],
                ),
            )
            conn.execute(
                "INSERT INTO conflict_history(conflict_id,status,recorded_at,actor_category,resolution_ref) VALUES (?,?,?,?,?)",
                (
                    row["id"],
                    status,
                    _iso(self.clock.now()),
                    actor.value,
                    resolution_ref,
                ),
            )
        return None

    def _reconcile_cutoff_in_tx(
        self,
        conn,
        account_id: str,
        entity_ref: str,
        *,
        actor: ActorCategory,
    ) -> EffectiveField | None:
        effective = self._compute_cutoff(conn, account_id, entity_ref)
        if effective is None:
            return None
        conflict_id = self._manage_conflict_in_tx(conn, effective, actor)
        effective = replace(effective, conflict_id=conflict_id)
        signature = self._effective_signature(effective)

        current = conn.execute(
            "SELECT * FROM effective_fields WHERE account_id=? AND entity_ref=? AND field_path=?",
            (account_id, entity_ref, _FIELD_ACTUAL_CUTOFF),
        ).fetchone()
        current_sig = None
        if current is not None:
            current_sig = {
                "state": current["state"],
                "value_type": current["selected_value_type"],
                "value": (
                    json.loads(current["selected_value_json"])
                    if current["selected_value_json"] is not None
                    else None
                ),
                "evidence_ids": json.loads(current["evidence_ids_json"]),
                "policy_version": current["policy_version"],
                "planning_projection": (
                    json.loads(current["planning_projection_json"])
                    if current["planning_projection_json"]
                    else None
                ),
                "admissible_cutoffs": json.loads(current["admissible_cutoffs_json"]),
                "reason": current["reason"],
                "override_id": current["override_id"],
            }
        if current_sig == signature:
            return effective

        material_keys = (
            "state",
            "value_type",
            "value",
            "policy_version",
            "planning_projection",
            "admissible_cutoffs",
            "reason",
        )
        material_changed = (
            current_sig is None
            or any(current_sig[key] != signature[key] for key in material_keys)
        )

        now = _iso(self.clock.now())
        conn.execute(
            "INSERT INTO effective_fields(account_id,entity_ref,field_path,state,selected_value_type,selected_value_json,evidence_ids_json,policy_version,planning_projection_json,admissible_cutoffs_json,reason,override_id,conflict_id,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(account_id,entity_ref,field_path) DO UPDATE SET "
            "state=excluded.state,selected_value_type=excluded.selected_value_type,"
            "selected_value_json=excluded.selected_value_json,evidence_ids_json=excluded.evidence_ids_json,"
            "policy_version=excluded.policy_version,planning_projection_json=excluded.planning_projection_json,"
            "admissible_cutoffs_json=excluded.admissible_cutoffs_json,reason=excluded.reason,"
            "override_id=excluded.override_id,conflict_id=excluded.conflict_id,updated_at=excluded.updated_at",
            (
                account_id,
                entity_ref,
                _FIELD_ACTUAL_CUTOFF,
                effective.state.value,
                effective.value_type.value if effective.value_type else None,
                _json(effective.value) if effective.value is not None else None,
                _json(list(effective.evidence_ids)),
                effective.policy_version,
                (
                    _json(signature["planning_projection"])
                    if signature["planning_projection"] is not None
                    else None
                ),
                _json(signature["admissible_cutoffs"]),
                effective.reason,
                effective.override_id,
                conflict_id,
                now,
            ),
        )
        conn.execute(
            "INSERT INTO effective_field_history(account_id,entity_ref,field_path,state,selected_value_type,selected_value_json,evidence_ids_json,policy_version,planning_projection_json,admissible_cutoffs_json,reason,override_id,conflict_id,recorded_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                account_id,
                entity_ref,
                _FIELD_ACTUAL_CUTOFF,
                effective.state.value,
                effective.value_type.value if effective.value_type else None,
                _json(effective.value) if effective.value is not None else None,
                _json(list(effective.evidence_ids)),
                effective.policy_version,
                (
                    _json(signature["planning_projection"])
                    if signature["planning_projection"] is not None
                    else None
                ),
                _json(signature["admissible_cutoffs"]),
                effective.reason,
                effective.override_id,
                conflict_id,
                now,
            ),
        )
        if material_changed:
            self.canonical._record_change(
                conn,
                account_id=account_id,
                entity_type="EFFECTIVE_FIELD",
                entity_id=f"{entity_ref}:{_FIELD_ACTUAL_CUTOFF}",
                action="RECONCILE_FIELD",
                actor=actor,
                payload={
                    "state": effective.state.value,
                    "policy_version": effective.policy_version,
                    "evidence_ids": list(effective.evidence_ids),
                    "conflict_id": conflict_id,
                },
            )
        return effective

    def reconcile_cutoff(
        self,
        *,
        account_id: str,
        entity_ref: str,
        actor: ActorCategory = ActorCategory.RECONCILER,
    ) -> EffectiveField | None:
        with self.canonical._tx() as conn:
            return self._reconcile_cutoff_in_tx(
                conn,
                account_id,
                entity_ref,
                actor=actor,
            )

    def get_effective_cutoff(
        self,
        account_id: str,
        entity_ref: str,
    ) -> EffectiveField | None:
        row = self.connection.execute(
            "SELECT * FROM effective_fields WHERE account_id=? AND entity_ref=? AND field_path=?",
            (account_id, entity_ref, _FIELD_ACTUAL_CUTOFF),
        ).fetchone()
        if row is None:
            return None
        value_type = (
            ObservationValueType(row["selected_value_type"])
            if row["selected_value_type"]
            else None
        )
        value = (
            json.loads(row["selected_value_json"])
            if row["selected_value_json"] is not None
            else None
        )
        projection = (
            _decode_cutoff(json.loads(row["planning_projection_json"]))
            if row["planning_projection_json"]
            else None
        )
        admissible = tuple(
            _decode_cutoff(item)
            for item in json.loads(row["admissible_cutoffs_json"])
        )
        return EffectiveField(
            account_id=row["account_id"],
            entity_ref=row["entity_ref"],
            field_path=row["field_path"],
            state=EffectiveFieldState(row["state"]),
            value_type=value_type,
            value=value,
            evidence_ids=tuple(json.loads(row["evidence_ids_json"])),
            policy_version=row["policy_version"],
            planning_projection=projection,
            admissible_cutoffs=admissible,
            reason=row["reason"],
            conflict_id=row["conflict_id"],
            override_id=row["override_id"],
        )

    def list_conflicts(
        self,
        account_id: str,
        *,
        entity_ref: str | None = None,
    ) -> list[dict[str, Any]]:
        self.canonical._require_account(account_id)
        sql = "SELECT * FROM reconciliation_conflicts WHERE account_id=?"
        args: list[Any] = [account_id]
        if entity_ref is not None:
            sql += " AND entity_ref=?"
            args.append(entity_ref)
        sql += " ORDER BY created_at,id"
        rows = self.connection.execute(sql, tuple(args)).fetchall()
        return [
            {
                "id": row["id"],
                "entity_ref": row["entity_ref"],
                "field_path": row["field_path"],
                "evidence_ids": tuple(json.loads(row["evidence_ids_json"])),
                "policy_version": row["policy_version"],
                "status": row["status"],
                "resolution_ref": row["resolution_ref"],
                "version": int(row["version"]),
            }
            for row in rows
        ]

    def record_source_removal(
        self,
        *,
        account_id: str,
        source_system_id: str,
        external_entity_id: str,
        extractor_id: str,
        actor: ActorCategory,
        source_revision: str | None = None,
        revision_order: int | None = None,
        source_record_id: str | None = None,
        observation_id: str | None = None,
        observed_at: datetime | None = None,
        content_hash: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Observation:
        observed_at = observed_at or self.clock.now()
        record: SourceRecord
        if source_record_id is not None:
            try:
                record = self.get_source_record(account_id, source_record_id)
            except EntityNotFound:
                record = self.add_source_record(
                    account_id=account_id,
                    source_system_id=source_system_id,
                    external_entity_id=external_entity_id,
                    source_revision=source_revision,
                    revision_order=revision_order,
                    observed_at=observed_at,
                    content_hash=content_hash,
                    actor=actor,
                    metadata={**dict(metadata or {}), "deletion_marker": True},
                    source_record_id=source_record_id,
                )
            if (
                record.source_system_id != source_system_id
                or record.external_entity_id != external_entity_id
            ):
                raise ValidationError(
                    "source removal idempotency collision for source_record_id"
                )
        else:
            record = self.add_source_record(
                account_id=account_id,
                source_system_id=source_system_id,
                external_entity_id=external_entity_id,
                source_revision=source_revision,
                revision_order=revision_order,
                observed_at=observed_at,
                content_hash=content_hash,
                actor=actor,
                metadata={**dict(metadata or {}), "deletion_marker": True},
            )

        active = self.connection.execute(
            "SELECT id,local_entity_id FROM source_bindings WHERE account_id=? AND source_system_id=? "
            "AND external_entity_id=? AND state='ACTIVE'",
            (account_id, source_system_id, external_entity_id),
        ).fetchone()
        if observation_id is not None and self.observation_exists(account_id, observation_id):
            observation = self.get_observation(account_id, observation_id)
            if observation.source_record_id != record.id:
                raise ValidationError(
                    "source removal idempotency collision for observation_id"
                )
        else:
            observation = self.add_observation(
                account_id=account_id,
                source_record_id=record.id,
                field_path="source_presence",
                value_type=ObservationValueType.SOURCE_REMOVED,
                value=None,
                extraction_certainty=ExtractionCertainty.EXACT,
                extractor_id=extractor_id,
                actor=actor,
                observed_at=observed_at,
                binding_id=active["id"] if active else None,
                observation_id=observation_id,
            )
        if active is not None:
            with self.canonical._tx() as conn:
                conn.execute(
                    "UPDATE source_bindings SET state='SOURCE_REMOVED',updated_at=?,version=version+1 WHERE id=? AND state='ACTIVE'",
                    (_iso(self.clock.now()), active["id"]),
                )
                if conn.execute("SELECT changes()").fetchone()[0]:
                    conn.execute(
                        "INSERT INTO binding_history(binding_id,state,recorded_at,actor_category,reason) VALUES (?,?,?,?,?)",
                        (
                            active["id"],
                            BindingState.SOURCE_REMOVED.value,
                            _iso(self.clock.now()),
                            actor.value,
                            "provider-removal-evidence",
                        ),
                    )
                    self._audit(
                        conn,
                        account_id=account_id,
                        action="SOURCE_ENTITY_REMOVED",
                        actor=actor,
                        entity_ref=active["local_entity_id"],
                        payload={"observation_id": observation.id},
                    )
                    self._reconcile_cutoff_in_tx(
                        conn,
                        account_id,
                        active["local_entity_id"],
                        actor=ActorCategory.RECONCILER,
                    )
        return observation

    def capture_task_idempotent(
        self,
        *,
        account_id: str,
        idempotency_key: str,
        title: str,
        category: ObligationCategory,
        importance: Importance,
        estimated_total_effort_minutes: int,
        remaining_effort_minutes: int,
        splittable: bool,
        actual_cutoff: HardCutoff,
        actor: ActorCategory,
        target_at: datetime | None = None,
    ) -> Task:
        if not idempotency_key:
            raise ValidationError("idempotency_key is required")
        payload = {
            "title": title,
            "category": category.value,
            "importance": importance.value,
            "estimated_total_effort_minutes": estimated_total_effort_minutes,
            "remaining_effort_minutes": remaining_effort_minutes,
            "splittable": splittable,
            "actual_cutoff": _encode_value(
                ObservationValueType.HARD_CUTOFF,
                actual_cutoff,
            ),
            "target_at": _iso(target_at),
        }
        digest = _payload_hash(payload)
        existing = self.connection.execute(
            "SELECT payload_hash,result_entity_id FROM idempotency_records "
            "WHERE account_id=? AND operation='CAPTURE_TASK' AND idempotency_key=?",
            (account_id, idempotency_key),
        ).fetchone()
        if existing is not None:
            if existing["payload_hash"] != digest:
                raise ValidationError(
                    "idempotency key reused with a different capture payload"
                )
            return self.canonical.get_task(
                account_id,
                existing["result_entity_id"],
            )

        obligation_id = str(
            uuid5(
                NAMESPACE_URL,
                f"student-execution-os:{account_id}:capture-task:{idempotency_key}",
            )
        )
        try:
            task = self.canonical.create_task(
                account_id=account_id,
                obligation_id=obligation_id,
                title=title,
                category=category,
                importance=importance,
                estimated_total_effort_minutes=estimated_total_effort_minutes,
                remaining_effort_minutes=remaining_effort_minutes,
                splittable=splittable,
                actual_cutoff=actual_cutoff,
                target_at=target_at,
                actor=actor,
            )
        except sqlite3.IntegrityError:
            try:
                task = self.canonical.get_task(account_id, obligation_id)
            except EntityNotFound:
                raise
            expected = (
                task.obligation.title == title
                and task.obligation.category is category
                and task.obligation.importance is importance
                and task.estimated_total_effort_minutes == estimated_total_effort_minutes
                and task.remaining_effort_minutes == remaining_effort_minutes
                and task.splittable is splittable
                and task.actual_cutoff == actual_cutoff
                and task.target_at == target_at
            )
            if not expected:
                raise ValidationError("deterministic capture identity collides with different task semantics")

        with self.canonical._tx() as conn:
            row = conn.execute(
                "SELECT payload_hash,result_entity_id FROM idempotency_records "
                "WHERE account_id=? AND operation='CAPTURE_TASK' AND idempotency_key=?",
                (account_id, idempotency_key),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO idempotency_records(account_id,operation,idempotency_key,payload_hash,result_entity_id,created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (
                        account_id,
                        "CAPTURE_TASK",
                        idempotency_key,
                        digest,
                        task.obligation.id,
                        _iso(self.clock.now()),
                    ),
                )
                self._audit(
                    conn,
                    account_id=account_id,
                    action="CAPTURE_TASK_IDEMPOTENCY",
                    actor=actor,
                    entity_ref=task.obligation.id,
                    payload={"idempotency_key": idempotency_key},
                )
            elif (
                row["payload_hash"] != digest
                or row["result_entity_id"] != task.obligation.id
            ):
                raise ValidationError("idempotency record collision")
        return task

    def apply_cutoff_projection(self, task: Task) -> Task:
        effective = self.get_effective_cutoff(
            task.obligation.account_id,
            task.obligation.id,
        )
        if effective is None:
            return task

        if effective.state is EffectiveFieldState.ABSENT:
            cutoff = HardCutoff.absent()
        elif effective.state in (
            EffectiveFieldState.RESOLVED,
            EffectiveFieldState.OVERRIDDEN,
        ):
            if effective.value_type is ObservationValueType.ABSENT:
                cutoff = HardCutoff.absent()
            elif (
                effective.value_type is ObservationValueType.HARD_CUTOFF
                and effective.value is not None
            ):
                cutoff = _decode_cutoff(effective.value)
            else:
                cutoff = effective.planning_projection or HardCutoff.unknown()
        elif (
            effective.state is EffectiveFieldState.CONFLICT
            and effective.planning_projection is not None
        ):
            cutoff = effective.planning_projection
        else:
            cutoff = HardCutoff.unknown()
        return replace(task, actual_cutoff=cutoff)

    @staticmethod
    def cutoff_has_passed(cutoff: HardCutoff, now: datetime) -> bool:
        return _cutoff_past(cutoff, now)
