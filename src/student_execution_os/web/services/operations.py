"""Typed canonical operations (the sync endpoint)."""
from __future__ import annotations

from typing import Any

from student_execution_os.domain.model import ActorCategory
from student_execution_os.sync.commands import SyncService

from .common import _jsonify

from .base import ApplicationService


class OperationService(ApplicationService):
    """Typed canonical operations (the sync endpoint)."""

    def sync(self, payload: dict[str, Any], *, actor: ActorCategory = ActorCategory.USER_UI) -> dict[str, Any]:
        operations = payload.get("operations")
        if not isinstance(operations, list):
            raise ValueError("operations must be a list")
        with self._repo() as repo:
            service = SyncService(
                repo, account_id=self.account_id, principal_id=self.principal.principal_id, actor=actor,
                now=self._now(),
            )
            return {
                "results": service.apply_batch(operations),
                "server_revision": repo.get_server_revision(self.account_id),
                "synced_at": _jsonify(self._now()),
            }
