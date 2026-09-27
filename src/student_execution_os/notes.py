from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from student_execution_os.domain.errors import EntityNotFound, ValidationError, VersionConflict
from student_execution_os.domain.model import ActorCategory
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository, _iso

MAX_NOTE_CONTENT = 20_000
MAX_TRANSCRIPT = 50_000
MAX_AUDIO_BYTES = 25 * 1024 * 1024


class SQLiteNoteRepository:
    """Canonical Notes storage.

    User-edited content and machine/speech transcript are intentionally separate.
    Original audio is immutable once attached: a failed transcription can never
    destroy the recording. Text mutations use optimistic versions, while offline
    operation ids are deduplicated by the existing client_operations boundary.
    """

    def __init__(self, canonical: SQLiteCanonicalRepository) -> None:
        self.repo = canonical

    @staticmethod
    def _clean_content(value: Any) -> str:
        text = str(value or "").strip()
        if len(text) > MAX_NOTE_CONTENT:
            raise ValidationError(f"note content is longer than {MAX_NOTE_CONTENT} characters")
        return text

    @staticmethod
    def _clean_transcript(value: Any) -> str | None:
        text = str(value or "").strip()
        if len(text) > MAX_TRANSCRIPT:
            raise ValidationError(f"transcript is longer than {MAX_TRANSCRIPT} characters")
        return text or None

    def _row(self, account_id: str, note_id: str):
        row = self.repo.connection.execute(
            "SELECT * FROM notes WHERE account_id=? AND id=?", (account_id, note_id)
        ).fetchone()
        if row is None:
            raise EntityNotFound("note not found")
        return row

    def _out(self, account_id: str, note_id: str) -> dict[str, Any]:
        row = self._row(account_id, note_id)
        audio = self.repo.connection.execute(
            "SELECT mime_type,original_name,size_bytes,sha256,created_at FROM note_audio "
            "WHERE account_id=? AND note_id=?", (account_id, note_id)
        ).fetchone()
        links = self.repo.connection.execute(
            "SELECT target_kind,target_id,created_at FROM note_links "
            "WHERE account_id=? AND note_id=? ORDER BY created_at,target_kind,target_id",
            (account_id, note_id),
        ).fetchall()
        return {
            "kind": "NOTE",
            "id": row["id"],
            "content": row["content"],
            "transcript": row["transcript"],
            "transcription_state": row["transcription_state"],
            "transcription_error": row["transcription_error"],
            "lifecycle_status": row["lifecycle_status"],
            "source_kind": row["source_kind"],
            "source_id": row["source_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "version": int(row["version"]),
            "audio": None if audio is None else dict(audio),
            "links": [dict(item) for item in links],
        }

    def get(self, account_id: str, note_id: str) -> dict[str, Any]:
        self.repo._require_account(account_id)
        return self._out(account_id, note_id)

    def list(self, account_id: str, *, q: str = "", include_archived: bool = False, limit: int = 200) -> list[dict[str, Any]]:
        self.repo._require_account(account_id)
        limit = max(1, min(int(limit), 500))
        needle = str(q or "").strip().casefold()
        sql = "SELECT id FROM notes WHERE account_id=?"
        params: list[Any] = [account_id]
        if not include_archived:
            sql += " AND lifecycle_status='ACTIVE'"
        # SQLite lower() is ASCII-oriented. A bounded Python casefold keeps RU/EN
        # search correct without introducing a second FTS/indexed source of truth.
        sql += " ORDER BY updated_at DESC,id LIMIT ?"
        params.append(500 if needle else limit)
        rows = self.repo.connection.execute(sql, tuple(params)).fetchall()
        items = [self._out(account_id, row["id"]) for row in rows]
        if needle:
            items = [
                item for item in items
                if needle in (item["content"] or "").casefold()
                or needle in (item["transcript"] or "").casefold()
            ]
        return items[:limit]

    def list_unlinked(self, account_id: str, *, limit: int = 3) -> list[dict[str, Any]]:
        rows = self.repo.connection.execute(
            "SELECT n.id FROM notes n WHERE n.account_id=? AND n.lifecycle_status='ACTIVE' "
            "AND NOT EXISTS (SELECT 1 FROM note_links l WHERE l.account_id=n.account_id AND l.note_id=n.id) "
            "ORDER BY n.updated_at DESC,n.id LIMIT ?",
            (account_id, max(1, min(int(limit), 20))),
        ).fetchall()
        return [self._out(account_id, row["id"]) for row in rows]

    def create(self, account_id: str, note_id: str, *, content: Any, source_kind: str,
               source_id: Any, now: datetime, actor: ActorCategory) -> dict[str, Any]:
        self.repo._require_account(account_id)
        text = self._clean_content(content)
        source_kind = str(source_kind or "CAPTURE").strip().upper()[:40] or "CAPTURE"
        source_id = None if source_id in (None, "") else str(source_id)[:200]
        with self.repo._tx() as conn:
            conn.execute(
                "INSERT INTO notes(id,account_id,content,source_kind,source_id,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (note_id, account_id, text, source_kind, source_id, _iso(now), _iso(now)),
            )
            self.repo._record_change(
                conn, account_id=account_id, entity_type="NOTE", entity_id=note_id,
                action="CREATE_NOTE", actor=actor,
            )
        return self._out(account_id, note_id)

    def update_content(self, account_id: str, note_id: str, expected_version: int,
                       content: Any, now: datetime, actor: ActorCategory) -> dict[str, Any]:
        text = self._clean_content(content)
        self._row(account_id, note_id)
        with self.repo._tx() as conn:
            cur = conn.execute(
                "UPDATE notes SET content=?,updated_at=?,version=version+1 "
                "WHERE account_id=? AND id=? AND version=?",
                (text, _iso(now), account_id, note_id, expected_version),
            )
            if cur.rowcount != 1:
                raise VersionConflict("note version changed before edit")
            self.repo._record_change(
                conn, account_id=account_id, entity_type="NOTE", entity_id=note_id,
                action="UPDATE_NOTE", actor=actor,
            )
        return self._out(account_id, note_id)

    def set_archived(self, account_id: str, note_id: str, expected_version: int,
                     archived: bool, now: datetime, actor: ActorCategory) -> dict[str, Any]:
        status = "ARCHIVED" if archived else "ACTIVE"
        self._row(account_id, note_id)
        with self.repo._tx() as conn:
            cur = conn.execute(
                "UPDATE notes SET lifecycle_status=?,updated_at=?,version=version+1 "
                "WHERE account_id=? AND id=? AND version=?",
                (status, _iso(now), account_id, note_id, expected_version),
            )
            if cur.rowcount != 1:
                raise VersionConflict("note version changed before lifecycle update")
            self.repo._record_change(
                conn, account_id=account_id, entity_type="NOTE", entity_id=note_id,
                action="ARCHIVE_NOTE" if archived else "UNARCHIVE_NOTE", actor=actor,
            )
        return self._out(account_id, note_id)

    def set_transcript(self, account_id: str, note_id: str, expected_version: int,
                       transcript: Any, state: str, error: str | None, now: datetime,
                       actor: ActorCategory) -> dict[str, Any]:
        if state not in {"READY", "FAILED", "NONE", "PENDING"}:
            raise ValidationError("unsupported transcription state")
        text = self._clean_transcript(transcript)
        if state == "READY" and text is None:
            raise ValidationError("READY transcript cannot be empty")
        self._row(account_id, note_id)
        with self.repo._tx() as conn:
            cur = conn.execute(
                "UPDATE notes SET transcript=?,transcription_state=?,transcription_error=?,updated_at=?,version=version+1 "
                "WHERE account_id=? AND id=? AND version=?",
                (text, state, None if not error else str(error)[:80], _iso(now), account_id, note_id, expected_version),
            )
            if cur.rowcount != 1:
                raise VersionConflict("note version changed before transcript update")
            self.repo._record_change(
                conn, account_id=account_id, entity_type="NOTE", entity_id=note_id,
                action="SET_NOTE_TRANSCRIPT", actor=actor,
            )
        return self._out(account_id, note_id)

    def save_audio(self, account_id: str, note_id: str, mime_type: str, original_name: str | None,
                   content: bytes, now: datetime, actor: ActorCategory) -> dict[str, Any]:
        self._row(account_id, note_id)
        if not content:
            raise ValidationError("audio content is empty")
        if len(content) > MAX_AUDIO_BYTES:
            raise ValidationError("audio is larger than 25 MiB")
        mime = str(mime_type or "").split(";", 1)[0].strip().lower()
        if not mime.startswith("audio/"):
            raise ValidationError("note attachment must be audio")
        if self.repo.connection.execute(
            "SELECT 1 FROM note_audio WHERE account_id=? AND note_id=?", (account_id, note_id)
        ).fetchone():
            raise ValidationError("note already has original audio; create another note to preserve provenance")
        with self.repo._tx() as conn:
            conn.execute(
                "INSERT INTO note_audio(note_id,account_id,mime_type,original_name,content,size_bytes,sha256,created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (note_id, account_id, mime, None if not original_name else str(original_name)[:255],
                 bytes(content), len(content), hashlib.sha256(content).hexdigest(), _iso(now)),
            )
            conn.execute(
                "UPDATE notes SET updated_at=?,version=version+1 WHERE account_id=? AND id=?",
                (_iso(now), account_id, note_id),
            )
            self.repo._record_change(
                conn, account_id=account_id, entity_type="NOTE", entity_id=note_id,
                action="ATTACH_NOTE_AUDIO", actor=actor,
            )
        return self._out(account_id, note_id)

    def audio(self, account_id: str, note_id: str) -> tuple[dict[str, Any], bytes]:
        self._row(account_id, note_id)
        row = self.repo.connection.execute(
            "SELECT mime_type,original_name,size_bytes,sha256,created_at,content FROM note_audio "
            "WHERE account_id=? AND note_id=?", (account_id, note_id)
        ).fetchone()
        if row is None:
            raise EntityNotFound("note audio not found")
        meta = {key: row[key] for key in ("mime_type","original_name","size_bytes","sha256","created_at")}
        return meta, bytes(row["content"])

    def link(self, account_id: str, note_id: str, target_kind: str, target_id: str, now: datetime) -> None:
        self._row(account_id, note_id)
        if target_kind not in {"TASK", "EVENT", "PROJECT"}:
            raise ValidationError("target_kind must be TASK, EVENT or PROJECT")
        if target_kind == "PROJECT":
            exists = self.repo.connection.execute(
                "SELECT 1 FROM projects WHERE account_id=? AND id=?", (account_id, target_id)
            ).fetchone()
        else:
            exists = self.repo.connection.execute(
                "SELECT 1 FROM obligations WHERE account_id=? AND id=? AND kind=?",
                (account_id, target_id, target_kind),
            ).fetchone()
        if exists is None:
            raise EntityNotFound("note conversion target not found")
        with self.repo._tx() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO note_links(account_id,note_id,target_kind,target_id,created_at) VALUES (?,?,?,?,?)",
                (account_id, note_id, target_kind, target_id, _iso(now)),
            )
            if cur.rowcount:
                self.repo._record_change(
                    conn, account_id=account_id, entity_type="NOTE", entity_id=note_id,
                    action="LINK_NOTE", actor=ActorCategory.USER_UI,
                    payload={"target_kind": target_kind, "target_id": target_id},
                )

    def delete(self, account_id: str, note_id: str, expected_version: int, now: datetime,
               actor: ActorCategory) -> None:
        row = self._row(account_id, note_id)
        if int(row["version"]) != int(expected_version):
            raise VersionConflict("note version changed before delete")
        with self.repo._tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO deleted_notes(account_id,note_id,deleted_at) VALUES (?,?,?)",
                (account_id, note_id, _iso(now)),
            )
            cur = conn.execute(
                "DELETE FROM notes WHERE account_id=? AND id=? AND version=?",
                (account_id, note_id, expected_version),
            )
            if cur.rowcount != 1:
                raise VersionConflict("note version changed before delete")
            self.repo._record_change(
                conn, account_id=account_id, entity_type="NOTE", entity_id=note_id,
                action="DELETE_NOTE", actor=actor,
            )

    def is_deleted(self, account_id: str, note_id: str) -> bool:
        return self.repo.connection.execute(
            "SELECT 1 FROM deleted_notes WHERE account_id=? AND note_id=?", (account_id, note_id)
        ).fetchone() is not None
