"""Notes and note audio."""
from __future__ import annotations

from typing import Any

from student_execution_os.domain.model import ActorCategory
from student_execution_os.notes import SQLiteNoteRepository

from .base import ApplicationService


class NoteService(ApplicationService):
    """Notes and note audio."""

    def notes(self, q: str = "", include_archived: bool = False) -> list[dict[str, Any]]:
        with self._repo() as repo:
            return SQLiteNoteRepository(repo).list(
                self.account_id, q=q, include_archived=include_archived, limit=200
            )

    def note(self, note_id: str) -> dict[str, Any]:
        with self._repo() as repo:
            return SQLiteNoteRepository(repo).get(self.account_id, note_id)

    def save_note_audio(self, note_id: str, mime_type: str, original_name: str | None, content: bytes) -> dict[str, Any]:
        with self._repo() as repo:
            return SQLiteNoteRepository(repo).save_audio(
                self.account_id, note_id, mime_type, original_name, content, self._now(), ActorCategory.USER_UI
            )

    def note_audio(self, note_id: str) -> tuple[dict[str, Any], bytes]:
        with self._repo() as repo:
            return SQLiteNoteRepository(repo).audio(self.account_id, note_id)
