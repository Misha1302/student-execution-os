"""Small parsers and limits shared by the application services."""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from student_execution_os.domain.errors import ValidationError
from student_execution_os.domain.model import CutoffBoundary, CutoffState, HardCutoff, TemporalPrecision



class _AccountLimiter:
    """At most ``limit`` calls per account per minute (outbound side effects)."""

    def __init__(self, limit: int) -> None:
        import threading
        self.limit = limit
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, account_id: str) -> bool:
        import time
        now = time.monotonic()
        with self._lock:
            hits = [at for at in self._hits.get(account_id, []) if now - at < 60]
            allowed = len(hits) < self.limit
            if allowed:
                hits.append(now)
            self._hits[account_id] = hits
            return allowed


TEST_NOTIFICATION_LIMITER = _AccountLimiter(3)


def _int_field(payload: dict[str, Any], name: str, default: int) -> int:
    value = payload.get(name, default)
    if isinstance(value, bool):
        raise ValidationError(f"{name} must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{name} must be an integer") from exc


def _occurrence_details(item) -> dict[str, Any]:
    """What a class looks like after its SOURCE and USER changes, and who changed it."""
    return {
        "title": item.title, "location_text": item.location_text, "teacher": item.teacher, "note": item.note,
        "cancelled_by": None if item.cancelled_by is None else item.cancelled_by.value,
        "cancel_reason": None if item.cancel_reason is None else item.cancel_reason.value,
        "changed_by": [layer.value for layer in item.changed_by],
    }


UPCOMING_HOURS = 12


def _jsonify(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {k: _jsonify(v) for k, v in asdict(value).items()}
    if isinstance(value, dict):
        return {str(k): _jsonify(v) for k, v in value.items()}
    if isinstance(value, (tuple, list, set)):
        return [_jsonify(v) for v in value]
    return value


def _dt(value: str | None) -> datetime | None:
    if value is None or value == "":
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("datetime must include an offset")
    return parsed


def _local_iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _cutoff(payload: dict[str, Any] | None) -> HardCutoff:
    payload = payload or {"state": "UNKNOWN"}
    state = CutoffState(payload.get("state", "UNKNOWN"))
    if state is CutoffState.ABSENT:
        return HardCutoff.absent()
    if state is CutoffState.UNKNOWN:
        precision = TemporalPrecision(payload.get("precision", TemporalPrecision.UNKNOWN.value))
        return HardCutoff.unknown(precision)
    at = _dt(payload.get("at"))
    if at is None:
        raise ValueError("KNOWN cutoff requires at")
    boundary = CutoffBoundary(payload.get("boundary", CutoffBoundary.INCLUSIVE.value))
    return HardCutoff.known(at, boundary)
