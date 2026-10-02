"""External sources: connectors and the academic schedule."""
from __future__ import annotations

from typing import Any

from .common import _int_field

from .base import ApplicationService


class ConnectorService(ApplicationService):
    """External sources: connectors and the academic schedule."""

    def connectors(self) -> list[dict[str, Any]]:
        from student_execution_os.connectors.service import list_connectors
        with self._repo() as repo:
            return list_connectors(repo, self.account_id)

    def sync_connector(self, connector_id: str) -> dict[str, Any]:
        from student_execution_os.connectors.service import sync_now
        with self._repo() as repo:
            return sync_now(repo, self.account_id, connector_id)

    def academic_schedule(self) -> dict[str, Any]:
        from student_execution_os.academic import AcademicScheduleService
        from student_execution_os.academic.credentials import academic_feed_cipher_from_environment

        with self._repo() as repo:
            return AcademicScheduleService(
                repo, account_id=self.account_id, cipher=academic_feed_cipher_from_environment()
            ).status()

    def connect_academic_schedule(self, payload: dict[str, Any]) -> dict[str, Any]:
        from student_execution_os.academic import AcademicScheduleService
        from student_execution_os.academic.credentials import academic_feed_cipher_from_environment

        with self._repo() as repo:
            return AcademicScheduleService(
                repo, account_id=self.account_id, cipher=academic_feed_cipher_from_environment()
            ).connect_url(
                url=str(payload.get("url", "")),
                display_name=str(payload.get("display_name", "Academic calendar")),
                default_timezone=str(payload.get("default_timezone", "Europe/Moscow")),
                sync_interval_minutes=_int_field(payload, "sync_interval_minutes", 60),
            )

    def import_academic_schedule(
        self, content: bytes, *, display_name: str, default_timezone: str
    ) -> dict[str, Any]:
        from student_execution_os.academic import AcademicScheduleService

        with self._repo() as repo:
            return AcademicScheduleService(repo, account_id=self.account_id).import_ics(
                content,
                display_name=display_name,
                default_timezone=default_timezone,
            )

    def refresh_academic_schedule(self) -> dict[str, Any]:
        from student_execution_os.academic import AcademicScheduleService
        from student_execution_os.academic.credentials import academic_feed_cipher_from_environment

        with self._repo() as repo:
            return AcademicScheduleService(
                repo, account_id=self.account_id, cipher=academic_feed_cipher_from_environment()
            ).refresh()

    def disconnect_academic_schedule(self) -> dict[str, Any]:
        from student_execution_os.academic import AcademicScheduleService

        with self._repo() as repo:
            return AcademicScheduleService(repo, account_id=self.account_id).disconnect()
