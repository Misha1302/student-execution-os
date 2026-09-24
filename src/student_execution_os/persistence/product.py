from __future__ import annotations

import base64
import binascii
import hashlib
import json
from pathlib import PurePath
from uuid import uuid4

from student_execution_os.domain.errors import EntityNotFound, ValidationError, VersionConflict
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository, _iso


MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_ACCOUNT_ATTACHMENT_BYTES = 50 * 1024 * 1024
UNSAFE_INLINE_MIMES = {"text/html", "application/xhtml+xml", "image/svg+xml"}


class SQLiteAttachmentRepository:
    def __init__(self, canonical: SQLiteCanonicalRepository) -> None:
        self.canonical = canonical

    def upload(self, account_id: str, payload: dict[str, object]) -> dict[str, object]:
        self.canonical._require_account(account_id)
        if set(payload) - {"owner_kind", "owner_id", "original_name", "mime_type", "content_base64"}:
            raise ValidationError("attachment request contains unknown fields")
        owner_kind = str(payload.get("owner_kind", ""))
        owner_id = str(payload.get("owner_id", ""))
        if owner_kind not in {"OBLIGATION", "SOURCE_RECORD"}:
            raise ValidationError("attachment owner_kind must be OBLIGATION or SOURCE_RECORD")
        if owner_kind == "OBLIGATION":
            self.canonical.get_obligation(account_id, owner_id)
        else:
            row = self.canonical.connection.execute(
                "SELECT 1 FROM source_records WHERE account_id=? AND id=?", (account_id, owner_id)
            ).fetchone()
            if row is None:
                raise EntityNotFound("source record not found")
        name = PurePath(str(payload.get("original_name", ""))).name.strip()
        if not name or len(name) > 255:
            raise ValidationError("attachment original_name is required and limited to 255 characters")
        mime = str(payload.get("mime_type") or "application/octet-stream").lower().strip()
        if len(mime) > 127 or "/" not in mime:
            raise ValidationError("invalid attachment MIME type")
        try:
            content = base64.b64decode(str(payload.get("content_base64", "")), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValidationError("attachment content_base64 is invalid") from exc
        if len(content) > MAX_ATTACHMENT_BYTES:
            raise ValidationError("attachment exceeds the 10 MiB file limit")
        used = int(self.canonical.connection.execute(
            "SELECT coalesce(sum(size_bytes),0) FROM attachment_blobs WHERE account_id=?", (account_id,)
        ).fetchone()[0])
        if used + len(content) > MAX_ACCOUNT_ATTACHMENT_BYTES:
            raise ValidationError("attachment account quota exceeds 50 MiB")
        now = self.canonical.clock.now()
        attachment_id, source_record_id, link_id = str(uuid4()), str(uuid4()), str(uuid4())
        digest = hashlib.sha256(content).hexdigest()
        source_system_id = "user-attachment-upload"
        with self.canonical._tx() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO source_systems(id,account_id,kind,policy_context_json,created_at) VALUES (?,?,?,?,?)",
                (source_system_id, account_id, "USER_ATTACHMENT_UPLOAD", "{}", _iso(now)),
            )
            conn.execute(
                "INSERT INTO source_records(id,account_id,source_system_id,observed_at,content_hash,metadata_json) VALUES (?,?,?,?,?,?)",
                (source_record_id, account_id, source_system_id, _iso(now), digest,
                 json.dumps({"original_name": name, "mime_type": mime, "size_bytes": len(content)}, sort_keys=True)),
            )
            conn.execute(
                "INSERT INTO attachment_blobs(id,account_id,sha256,mime_type,original_name,size_bytes,content,source_record_id,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (attachment_id, account_id, digest, mime, name, len(content), content, source_record_id, _iso(now)),
            )
            conn.execute(
                "INSERT INTO attachment_links(id,account_id,attachment_id,owner_kind,owner_id,created_at) VALUES (?,?,?,?,?,?)",
                (link_id, account_id, attachment_id, owner_kind, owner_id, _iso(now)),
            )
        return self.get_metadata(account_id, attachment_id, link_id=link_id)

    def get_metadata(self, account_id: str, attachment_id: str, *, link_id: str | None = None) -> dict[str, object]:
        query = (
            "SELECT b.id,b.sha256,b.mime_type,b.original_name,b.size_bytes,b.source_record_id,b.created_at,"
            "l.id AS link_id,l.owner_kind,l.owner_id,l.version AS link_version "
            "FROM attachment_blobs b JOIN attachment_links l ON l.account_id=b.account_id AND l.attachment_id=b.id "
            "WHERE b.account_id=? AND b.id=?"
        )
        params: tuple[object, ...] = (account_id, attachment_id)
        if link_id is not None:
            query += " AND l.id=?"
            params += (link_id,)
        row = self.canonical.connection.execute(query + " ORDER BY l.created_at LIMIT 1", params).fetchone()
        if row is None:
            raise EntityNotFound("attachment not found")
        return dict(row)

    def list(self, account_id: str, owner_kind: str, owner_id: str) -> list[dict[str, object]]:
        rows = self.canonical.connection.execute(
            "SELECT b.id,b.sha256,b.mime_type,b.original_name,b.size_bytes,b.source_record_id,b.created_at,"
            "l.id AS link_id,l.owner_kind,l.owner_id,l.version AS link_version "
            "FROM attachment_links l JOIN attachment_blobs b ON b.account_id=l.account_id AND b.id=l.attachment_id "
            "WHERE l.account_id=? AND l.owner_kind=? AND l.owner_id=? ORDER BY l.created_at,l.id",
            (account_id, owner_kind, owner_id),
        ).fetchall()
        return [dict(row) for row in rows]

    def download(self, account_id: str, attachment_id: str) -> tuple[dict[str, object], bytes]:
        row = self.canonical.connection.execute(
            "SELECT id,sha256,mime_type,original_name,size_bytes,source_record_id,created_at,content "
            "FROM attachment_blobs WHERE account_id=? AND id=?", (account_id, attachment_id)
        ).fetchone()
        if row is None:
            raise EntityNotFound("attachment not found")
        metadata = {key: row[key] for key in row.keys() if key != "content"}
        return metadata, bytes(row["content"])

    def unlink(self, account_id: str, link_id: str, expected_version: int) -> dict[str, object]:
        row = self.canonical.connection.execute(
            "SELECT * FROM attachment_links WHERE account_id=? AND id=?", (account_id, link_id)
        ).fetchone()
        if row is None:
            raise EntityNotFound("attachment link not found")
        if int(row["version"]) != expected_version:
            raise VersionConflict("attachment link version changed")
        with self.canonical._tx() as conn:
            cur = conn.execute(
                "DELETE FROM attachment_links WHERE account_id=? AND id=? AND version=?",
                (account_id, link_id, expected_version),
            )
            if cur.rowcount != 1:
                raise VersionConflict("attachment link changed before unlink")
        return {"id": link_id, "unlinked": True}


SYSTEM_VIEWS = (
    {"id": "system-active", "name": "Active", "system": True, "definition": {"schema_version": 1, "filters": {"status": ["ACTIVE"]}, "sort": [{"field": "deadline", "direction": "asc"}], "grouping": "status"}},
    {"id": "system-risk", "name": "Risk", "system": True, "definition": {"schema_version": 1, "filters": {"risk": ["AT_RISK", "CRITICAL", "IMPOSSIBLE", "OVERDUE"]}, "sort": [{"field": "risk", "direction": "desc"}], "grouping": "risk"}},
    {"id": "system-needs-estimate", "name": "Needs estimate", "system": True, "definition": {"schema_version": 1, "filters": {"status": ["DRAFT"]}, "sort": [{"field": "created_at", "direction": "asc"}], "grouping": "status"}},
)


def validate_view_definition(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {"schema_version", "filters", "sort", "grouping"}:
        raise ValidationError("saved view definition must contain exactly schema_version, filters, sort, grouping")
    if value["schema_version"] != 1:
        raise ValidationError("unsupported saved view schema_version")
    filters = value["filters"]
    if not isinstance(filters, dict) or not set(filters).issubset({"status", "risk", "deadline_period", "category", "project"}):
        raise ValidationError("saved view contains an unknown filter")
    if any(not isinstance(item, list) or not all(isinstance(v, str) for v in item) for item in filters.values()):
        raise ValidationError("saved view filters must be string arrays")
    sort = value["sort"]
    if not isinstance(sort, list) or not 1 <= len(sort) <= 3:
        raise ValidationError("saved view sort requires one to three fields")
    for item in sort:
        if not isinstance(item, dict) or set(item) != {"field", "direction"}:
            raise ValidationError("saved view sort item is invalid")
        if item["field"] not in {"title", "status", "risk", "deadline", "created_at"} or item["direction"] not in {"asc", "desc"}:
            raise ValidationError("saved view sort field or direction is invalid")
    if value["grouping"] not in {"status", "risk", "deadline_period", "category", "project", None}:
        raise ValidationError("saved view grouping is invalid")
    return value


class SQLiteSavedViewRepository:
    def __init__(self, canonical: SQLiteCanonicalRepository) -> None:
        self.canonical = canonical

    def list(self, account_id: str) -> list[dict[str, object]]:
        rows = self.canonical.connection.execute(
            "SELECT * FROM saved_task_views WHERE account_id=? ORDER BY name,id", (account_id,)
        ).fetchall()
        custom = [{"id": row["id"], "name": row["name"], "system": False,
                   "definition": json.loads(row["definition_json"]), "version": int(row["version"])} for row in rows]
        return [*SYSTEM_VIEWS, *custom]

    def create(self, account_id: str, payload: dict[str, object]) -> dict[str, object]:
        if set(payload) != {"name", "definition"}:
            raise ValidationError("saved view create request contains unknown or missing fields")
        name = str(payload["name"]).strip()
        if not name or len(name) > 80:
            raise ValidationError("saved view name is required and limited to 80 characters")
        definition = validate_view_definition(payload["definition"])
        view_id, now = str(uuid4()), self.canonical.clock.now()
        with self.canonical._tx() as conn:
            conn.execute(
                "INSERT INTO saved_task_views(id,account_id,name,definition_json,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                (view_id, account_id, name, json.dumps(definition, sort_keys=True), _iso(now), _iso(now)),
            )
        return next(item for item in self.list(account_id) if item["id"] == view_id)

    def update(self, account_id: str, view_id: str, payload: dict[str, object]) -> dict[str, object]:
        if set(payload) - {"expected_version", "name", "definition"} or "expected_version" not in payload:
            raise ValidationError("saved view update request contains unknown or missing fields")
        row = self.canonical.connection.execute(
            "SELECT * FROM saved_task_views WHERE account_id=? AND id=?", (account_id, view_id)
        ).fetchone()
        if row is None:
            raise EntityNotFound("saved view not found")
        expected = int(payload["expected_version"])
        if int(row["version"]) != expected:
            raise VersionConflict("saved view version changed")
        name = str(payload.get("name", row["name"])).strip()
        definition = validate_view_definition(payload.get("definition", json.loads(row["definition_json"])))
        with self.canonical._tx() as conn:
            cur = conn.execute(
                "UPDATE saved_task_views SET name=?,definition_json=?,version=version+1,updated_at=? "
                "WHERE account_id=? AND id=? AND version=?",
                (name, json.dumps(definition, sort_keys=True), _iso(self.canonical.clock.now()), account_id, view_id, expected),
            )
            if cur.rowcount != 1:
                raise VersionConflict("saved view changed before commit")
        return next(item for item in self.list(account_id) if item["id"] == view_id)

    def delete(self, account_id: str, view_id: str, expected_version: int) -> dict[str, object]:
        with self.canonical._tx() as conn:
            cur = conn.execute(
                "DELETE FROM saved_task_views WHERE account_id=? AND id=? AND version=?",
                (account_id, view_id, expected_version),
            )
            if cur.rowcount != 1:
                exists = conn.execute("SELECT 1 FROM saved_task_views WHERE account_id=? AND id=?", (account_id, view_id)).fetchone()
                if exists:
                    raise VersionConflict("saved view version changed")
                raise EntityNotFound("saved view not found")
        return {"id": view_id, "deleted": True}
