"""Notes and captured text/audio."""
from __future__ import annotations

from typing import Any

from student_execution_os.domain.errors import ValidationError
from student_execution_os.notes import SQLiteNoteRepository

from ..primitives import APPLIED, NOOP, _ID, Outcome

from .base import CommandHandler, Handler


class NoteCommandHandler(CommandHandler):
    """Notes and captured text/audio."""

    def operations(self) -> dict[str, Handler]:
        return {
            "note.create": self.note_create,
            "note.update": self.note_update,
            "note.archive": self.note_archive,
            "note.unarchive": self.note_unarchive,
            "note.delete": self.note_delete,
            "note.transcript.set": self.note_transcript_set,
            "note.transcript.fail": self.note_transcript_fail,
            "note.link": self.note_link,
        }

    def _notes(self) -> SQLiteNoteRepository:
        return SQLiteNoteRepository(self.repo)

    def _note_out(self, note_id: str, status: str = APPLIED, code: str | None = None,
                  message: str | None = None) -> Outcome:
        return Outcome(status, self._notes().get(self.account_id, note_id), code, message)

    def note_create(self, note_id: str, payload: dict[str, Any]) -> Outcome:
        if not _ID.match(note_id):
            raise ValidationError("note id must be a client-generated identifier (8-128 safe characters)")
        owner = self.repo.connection.execute("SELECT account_id FROM notes WHERE id=?", (note_id,)).fetchone()
        if owner is not None:
            if owner["account_id"] != self.account_id:
                raise ValidationError("note id is already in use")
            return self._note_out(note_id, NOOP, "ALREADY_EXISTS", "note already exists")
        note = self._notes().create(
            self.account_id,
            note_id,
            content=payload.get("content", ""),
            source_kind=str(payload.get("source_kind") or "CAPTURE"),
            source_id=payload.get("source_id"),
            now=self.now,
            actor=self._capture_actor(payload),
        )
        return Outcome(APPLIED, note)

    def note_update(self, note_id: str, payload: dict[str, Any]) -> Outcome:
        unknown = set(payload) - {"expected_version", "content"}
        if unknown:
            raise ValidationError("fields cannot be edited: " + ", ".join(sorted(unknown)))
        if "expected_version" not in payload:
            raise ValidationError("expected_version is required for note edits")
        note = self._notes().update_content(
            self.account_id, note_id, int(payload["expected_version"]),
            payload.get("content", ""), self.now, self.actor,
        )
        return Outcome(APPLIED, note)

    def note_archive(self, note_id: str, payload: dict[str, Any]) -> Outcome:
        current = self._notes().get(self.account_id, note_id)
        if current["lifecycle_status"] == "ARCHIVED":
            return Outcome(NOOP, current, "ALREADY_ARCHIVED")
        return Outcome(APPLIED, self._notes().set_archived(
            self.account_id, note_id, int(payload.get("expected_version", current["version"])),
            True, self.now, self.actor,
        ))

    def note_unarchive(self, note_id: str, payload: dict[str, Any]) -> Outcome:
        current = self._notes().get(self.account_id, note_id)
        if current["lifecycle_status"] == "ACTIVE":
            return Outcome(NOOP, current, "ALREADY_ACTIVE")
        return Outcome(APPLIED, self._notes().set_archived(
            self.account_id, note_id, int(payload.get("expected_version", current["version"])),
            False, self.now, self.actor,
        ))

    def note_delete(self, note_id: str, payload: dict[str, Any]) -> Outcome:
        current = self._notes().get(self.account_id, note_id)
        self._notes().delete(
            self.account_id, note_id, int(payload.get("expected_version", current["version"])),
            self.now, self.actor,
        )
        return Outcome(APPLIED, {"kind": "NOTE", "id": note_id, "deleted": True})

    def note_transcript_set(self, note_id: str, payload: dict[str, Any]) -> Outcome:
        if "expected_version" not in payload:
            raise ValidationError("expected_version is required for transcript edits")
        return Outcome(APPLIED, self._notes().set_transcript(
            self.account_id, note_id, int(payload["expected_version"]),
            payload.get("transcript"), "READY", None, self.now, self.actor,
        ))

    def note_transcript_fail(self, note_id: str, payload: dict[str, Any]) -> Outcome:
        if "expected_version" not in payload:
            raise ValidationError("expected_version is required for transcript state changes")
        return Outcome(APPLIED, self._notes().set_transcript(
            self.account_id, note_id, int(payload["expected_version"]),
            None, "FAILED", str(payload.get("error_code") or "TRANSCRIPTION_FAILED")[:80],
            self.now, self.actor,
        ))

    def note_link(self, note_id: str, payload: dict[str, Any]) -> Outcome:
        kind = str(payload.get("target_kind") or "").upper()
        target_id = str(payload.get("target_id") or "")
        self._notes().link(self.account_id, note_id, kind, target_id, self.now)
        return self._note_out(note_id)
