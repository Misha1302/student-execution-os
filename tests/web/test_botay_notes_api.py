from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from student_execution_os.domain.clock import FrozenClock
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository
from student_execution_os.reliability import SQLiteDataLifecycle
from student_execution_os.web.app import create_app
from tests.asgi_client import TestClient

NOW = datetime(2026, 9, 28, 9, 0, tzinfo=timezone.utc)
ACCOUNT = "botay-notes"
OTHER = "botay-other"


class BotayNotesApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = str(Path(self.temp.name) / "botay.sqlite")
        with SQLiteCanonicalRepository(self.database, clock=FrozenClock(NOW)) as repo:
            repo.initialize()
            repo.create_account(ACCOUNT)
            repo.create_account(OTHER)
        self.client = TestClient(create_app(self.database, account_id=ACCOUNT, principal_id="u", now=lambda: NOW))
        self.other = TestClient(create_app(self.database, account_id=OTHER, principal_id="other", now=lambda: NOW))

    def tearDown(self):
        self.temp.cleanup()

    def sync(self, *operations):
        response = self.client.post("/api/v1/sync", json={"operations": list(operations)})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["results"]

    def test_text_note_crud_replay_conflict_archive_and_account_isolation(self):
        create = {"op_id": "op-note-create-0001", "type": "note.create", "entity_id": "note-0001",
                  "payload": {"content": "Идея: dependency graph", "source_kind": "CAPTURE"}}
        first = self.sync(create)[0]
        self.assertEqual(first["status"], "APPLIED")
        self.assertEqual(first["entity"]["version"], 1)
        replay = self.sync(create)[0]
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["entity"]["id"], "note-0001")

        listed = self.client.get("/api/v1/notes").json()
        self.assertEqual([n["id"] for n in listed], ["note-0001"])
        self.assertEqual(self.other.get("/api/v1/notes/note-0001").status_code, 404)
        self.assertEqual([n["id"] for n in self.client.get("/api/v1/notes?q=ИДЕЯ").json()], ["note-0001"])

        updated = self.sync({
            "op_id": "op-note-update-0001", "type": "note.update", "entity_id": "note-0001",
            "payload": {"expected_version": 1, "content": "Edited by the user"},
        })[0]
        self.assertEqual((updated["status"], updated["entity"]["version"]), ("APPLIED", 2))
        conflict = self.sync({
            "op_id": "op-note-update-stale", "type": "note.update", "entity_id": "note-0001",
            "payload": {"expected_version": 1, "content": "stale edit"},
        })[0]
        self.assertEqual((conflict["status"], conflict["code"]), ("CONFLICT", "VERSION_CONFLICT"))

        archived = self.sync({
            "op_id": "op-note-archive-1", "type": "note.archive", "entity_id": "note-0001",
            "payload": {"expected_version": 2},
        })[0]["entity"]
        self.assertEqual(archived["lifecycle_status"], "ARCHIVED")
        self.assertEqual(self.client.get("/api/v1/notes").json(), [])
        all_notes = self.client.get("/api/v1/notes?include_archived=true").json()
        self.assertEqual([n["id"] for n in all_notes], ["note-0001"])

        restored = self.sync({
            "op_id": "op-note-restore-1", "type": "note.unarchive", "entity_id": "note-0001",
            "payload": {"expected_version": archived["version"]},
        })[0]["entity"]
        export = self.client.get("/api/v1/account/export").json()
        self.assertEqual(export["tables"]["notes"][0]["content"], "Edited by the user")

        deleted = self.sync({
            "op_id": "op-note-delete-0001", "type": "note.delete", "entity_id": "note-0001",
            "payload": {"expected_version": restored["version"]},
        })[0]
        self.assertTrue(deleted["entity"]["deleted"])
        late = self.sync({
            "op_id": "op-note-late-update", "type": "note.update", "entity_id": "note-0001",
            "payload": {"expected_version": restored["version"], "content": "must not resurrect"},
        })[0]
        self.assertEqual((late["status"], late["code"]), ("NOOP", "DELETED"))
        self.assertEqual(self.client.get("/api/v1/notes?include_archived=true").json(), [])

    def test_original_audio_survives_transcription_failure_and_backup_restore(self):
        created = self.sync({
            "op_id": "op-note-voice-create", "type": "note.create", "entity_id": "note-voice",
            "payload": {"content": "", "source_kind": "VOICE"},
        })[0]["entity"]
        audio = b"RIFF-not-really-a-wave-but-preserved-verbatim"
        uploaded = self.client.put(
            "/api/v1/notes/note-voice/audio",
            content=audio,
            headers={"content-type": "audio/wav", "x-filename": "voice.wav"},
        )
        self.assertEqual(uploaded.status_code, 201, uploaded.text)
        note = uploaded.json()
        self.assertEqual(note["audio"]["size_bytes"], len(audio))
        self.assertEqual(self.client.get("/api/v1/notes/note-voice/audio").content, audio)

        failed = self.sync({
            "op_id": "op-note-transcript-fail", "type": "note.transcript.fail", "entity_id": "note-voice",
            "payload": {"expected_version": note["version"], "error_code": "PROVIDER_UNAVAILABLE"},
        })[0]["entity"]
        self.assertEqual(failed["transcription_state"], "FAILED")
        self.assertIsNone(failed["transcript"])
        self.assertIsNotNone(failed["audio"])

        export = self.client.get("/api/v1/account/export").json()
        self.assertEqual(export["tables"]["note_audio"][0]["content"]["encoding"], "base64")

        backup = Path(self.temp.name) / "backup.sqlite"
        SQLiteDataLifecycle(self.database, now=lambda: NOW).create_backup(backup)
        restored = Path(self.temp.name) / "restored.sqlite"
        SQLiteDataLifecycle.restore_backup(backup, restored)
        restored_client = TestClient(create_app(str(restored), account_id=ACCOUNT, principal_id="u", now=lambda: NOW))
        self.assertEqual(restored_client.get("/api/v1/notes/note-voice/audio").content, audio)

        deleted = self.sync({
            "op_id": "op-note-voice-delete", "type": "note.delete", "entity_id": "note-voice",
            "payload": {"expected_version": failed["version"]},
        })[0]
        self.assertEqual(deleted["status"], "APPLIED")
        with SQLiteCanonicalRepository(self.database, clock=FrozenClock(NOW)) as repo:
            self.assertEqual(repo.connection.execute(
                "SELECT count(*) FROM note_audio WHERE account_id=?", (ACCOUNT,)
            ).fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
