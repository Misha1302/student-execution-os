from __future__ import annotations

import json
from typing import Mapping
from uuid import uuid4

from .sqlite import SQLiteCanonicalRepository, _iso


class SQLiteOperationalMetrics:
    """Small, payload-free operational metric sink for the local deployment."""

    def __init__(self, canonical: SQLiteCanonicalRepository) -> None:
        self.canonical = canonical

    def record(
        self,
        metric_name: str,
        value: float = 1,
        *,
        account_id: str | None = None,
        correlation_id: str | None = None,
        dimensions: Mapping[str, str | int | float | bool | None] | None = None,
    ) -> None:
        if not metric_name or len(metric_name) > 128:
            raise ValueError("metric_name must be non-empty and at most 128 characters")
        safe_dimensions = dict(dimensions or {})
        if len(safe_dimensions) > 16 or any(len(str(key)) > 64 or len(str(value)) > 128 for key, value in safe_dimensions.items()):
            raise ValueError("metric dimensions exceed the bounded operational schema")
        if account_id is not None:
            self.canonical._require_account(account_id)
        with self.canonical._tx() as conn:
            conn.execute(
                "INSERT INTO operational_metrics(account_id,correlation_id,metric_name,value,dimensions_json,recorded_at) "
                "VALUES (?,?,?,?,?,?)",
                (
                    account_id,
                    correlation_id or str(uuid4()),
                    metric_name,
                    float(value),
                    json.dumps(safe_dimensions, sort_keys=True, separators=(",", ":")),
                    _iso(self.canonical.clock.now()),
                ),
            )
