from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository
from student_execution_os.planning.model import FeasibilityStatus, PlanBlock, PlanBlockType, PlanSnapshot


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


@dataclass(frozen=True)
class SQLitePlanStore:
    repository: SQLiteCanonicalRepository

    def save(self, plan: PlanSnapshot) -> None:
        conn = self.repository.connection
        self.repository._require_account(plan.account_id)
        exists = conn.execute("SELECT input_hash FROM plan_snapshots WHERE id=?", (plan.id,)).fetchone()
        if exists is not None and exists["input_hash"] != plan.input_hash:
            raise RuntimeError("plan id collision")
        if exists is None:
            conn.execute(
                "INSERT INTO plan_snapshots(id,account_id,plan_revision,input_server_revision,input_hash,horizon_start,horizon_end,feasibility_status,generated_at,explanations_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (plan.id, plan.account_id, plan.plan_revision, plan.input_server_revision, plan.input_hash,
                 plan.horizon_start.isoformat(), plan.horizon_end.isoformat(), plan.feasibility_status.value,
                 plan.generated_at.isoformat(), json.dumps(plan.explanations)),
            )
            for block in plan.blocks:
                conn.execute(
                    "INSERT INTO plan_blocks(id,plan_id,block_type,starts_at,ends_at,obligation_id,source_constraint_ids_json,source_event_id,explanation) VALUES (?,?,?,?,?,?,?,?,?)",
                    (block.id, plan.id, block.type.value, block.starts_at.isoformat(), block.ends_at.isoformat(),
                     block.obligation_id, json.dumps(block.source_constraint_ids), block.source_event_id, block.explanation),
                )
        conn.execute(
            "INSERT INTO current_plans(account_id,plan_id) VALUES (?,?) ON CONFLICT(account_id) DO UPDATE SET plan_id=excluded.plan_id",
            (plan.account_id, plan.id),
        )
        conn.commit()

    def get(self, account_id: str, plan_id: str) -> PlanSnapshot | None:
        row = self.repository.connection.execute(
            "SELECT * FROM plan_snapshots WHERE account_id=? AND id=?", (account_id, plan_id)
        ).fetchone()
        if row is None:
            return None
        block_rows = self.repository.connection.execute(
            "SELECT * FROM plan_blocks WHERE plan_id=? ORDER BY starts_at,id", (plan_id,)
        ).fetchall()
        blocks = tuple(PlanBlock(
            starts_at=_dt(b["starts_at"]), ends_at=_dt(b["ends_at"]), id=b["id"], type=PlanBlockType(b["block_type"]),
            obligation_id=b["obligation_id"], source_constraint_ids=tuple(json.loads(b["source_constraint_ids_json"])),
            source_event_id=b["source_event_id"], explanation=b["explanation"],
        ) for b in block_rows)
        return PlanSnapshot(
            id=row["id"], account_id=row["account_id"], plan_revision=row["plan_revision"],
            input_server_revision=int(row["input_server_revision"]), input_hash=row["input_hash"],
            horizon_start=_dt(row["horizon_start"]), horizon_end=_dt(row["horizon_end"]),
            feasibility_status=FeasibilityStatus(row["feasibility_status"]), generated_at=_dt(row["generated_at"]),
            blocks=blocks, explanations=tuple(json.loads(row["explanations_json"])),
        )

    def get_latest(self, account_id: str) -> PlanSnapshot | None:
        row = self.repository.connection.execute("SELECT plan_id FROM current_plans WHERE account_id=?", (account_id,)).fetchone()
        return None if row is None else self.get(account_id, row["plan_id"])

    def get_current(self, account_id: str, input_hash: str) -> PlanSnapshot | None:
        plan = self.get_latest(account_id)
        return plan if plan is not None and plan.input_hash == input_hash else None

    def history_ids(self, account_id: str) -> tuple[str, ...]:
        rows = self.repository.connection.execute(
            "SELECT id FROM plan_snapshots WHERE account_id=? ORDER BY generated_at,id", (account_id,)
        ).fetchall()
        return tuple(row["id"] for row in rows)
