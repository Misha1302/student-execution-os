"""Provenance of v22_populated_by_r2.sqlite.gz (schema v22, written by real old code).

Generated from a checkout of d85ff2f (R2 merge) with:
    PYTHONPATH=src:. python populate_v22_with_r2.py v22.sqlite
then checkpointed (journal_mode=DELETE, VACUUM) and gzipped. Kept for reproducibility;
the upgrade test in tests/integration/test_r11_global_lifecycle.py reads the .gz.
"""
import sys
from datetime import datetime, timezone
from student_execution_os.web.app import create_app
from student_execution_os.web.auth import AuthConfig
from tests.asgi_client import TestClient

NOW = datetime(2026, 9, 21, 7, 0, tzinfo=timezone.utc)
db = sys.argv[1]
client = TestClient(create_app(db, auth=AuthConfig(password_scrypt_n=2**10), now=lambda: NOW))
accounts = {}
for login in ("legacy-anna", "legacy-boris"):
    body = client.post("/api/v1/auth/register", json={"login": login, "password": "correct horse"}).json()
    accounts[login] = {"Authorization": f"Bearer {body['token']}"}

def sync(who, *ops):
    results = client.post("/api/v1/sync", headers=accounts[who], json={"operations": list(ops)}).json()["results"]
    for r in results:
        assert r["status"] in ("APPLIED", "NOOP"), r
    return results

sync("legacy-anna",
     {"op_id": "v22-task-0001", "type": "task.create", "entity_id": "v22-task-essay",
      "payload": {"title": "Эссе по истории", "estimated_total_effort_minutes": 120,
                  "actual_cutoff": {"state": "KNOWN", "at": "2026-09-30T20:59:00+00:00"}, "importance": "HIGH"}},
     {"op_id": "v22-task-0002", "type": "task.progress", "entity_id": "v22-task-essay", "payload": {"minutes": 30}},
     {"op_id": "v22-task-0003", "type": "task.create", "entity_id": "v22-task-done",
      "payload": {"title": "Сдать справку", "estimated_total_effort_minutes": 15}},
     {"op_id": "v22-task-0004", "type": "task.complete", "entity_id": "v22-task-done", "payload": {}},
     {"op_id": "v22-event-001", "type": "event.create", "entity_id": "v22-event-meet",
      "payload": {"title": "Встреча с научруком", "starts_at": "2026-09-23T12:00:00+00:00",
                  "ends_at": "2026-09-23T13:00:00+00:00", "remind_before_minutes": 30}},
     {"op_id": "v22-note-0001", "type": "note.create", "entity_id": "v22-note-ideas",
      "payload": {"content": "Идеи для курсовой: графы и расписания"}},
     {"op_id": "v22-rem-00001", "type": "reminder.create", "entity_id": "v22-reminder-1",
      "payload": {"title": "Купить тетради", "remind_at": "2026-09-22T15:00:00+00:00"}},
     {"op_id": "v22-proj-0001", "type": "project.create", "entity_id": "v22-project-1",
      "payload": {"title": "Курсовая работа"}},
     )
sync("legacy-boris",
     {"op_id": "v22-task-b001", "type": "task.create", "entity_id": "v22-task-boris",
      "payload": {"title": "Лабораторная по физике", "estimated_total_effort_minutes": 90}})
print("populated", db)
