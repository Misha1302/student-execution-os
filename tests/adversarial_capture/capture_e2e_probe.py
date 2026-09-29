from __future__ import annotations

import json
from pathlib import Path

from tests.browser.offline_e2e import ACCOUNT, OfflineEndToEndTest


OUT = Path("artifacts/capture-adversarial/ui-api-e2e.json")


SCENARIOS = [
    {"id": "simple-event", "input": "Завтра в 18:00 созвон с Ариадной на полчаса",
     "oracle": {"kind": "EVENT", "title": "Созвон с Ариадной", "duration_minutes": 30}},
    {"id": "event-reminder", "input": "Завтра в 19:00 встреча с Димой на час, напомни за 15 минут до начала",
     "oracle": {"kind": "EVENT", "title": "Встреча с Димой", "duration_minutes": 60, "remind_before_minutes": 15}},
    {"id": "event-duration-reminder", "input": "Лекция по матану завтра в 12:00 на 90 минут. Напомни за 30 минут",
     "oracle": {"kind": "EVENT", "title": "Лекция по матану", "duration_minutes": 90, "remind_before_minutes": 30}},
    {"id": "event-arbitrary-50", "input": "созвон с ариадной в 18:00 завтра на пол часа.\nНапомни за 50 минут до начала",
     "oracle": {"kind": "EVENT", "title": "Созвон с Ариадной", "duration_minutes": 30, "remind_before_minutes": 50, "deadline": None}},
    {"id": "task-deadline-reminder", "input": "Сдать лабу завтра до 20:00, 2 часа работы. Напомни завтра в 16:00 начать",
     "oracle": {"kind": "TASK", "title": "Сдать лабу", "effort_minutes": 120, "deadline_role_distinct_from_reminder": True}},
    {"id": "standalone-reminder", "input": "Напомни завтра в 17:00 позвонить маме",
     "oracle": {"kind": "REMINDER", "title": "Позвонить маме", "deadline": None}},
    {"id": "preparation-remains-task", "input": "Подготовиться к встрече с Димой завтра до 18:00, займёт час",
     "oracle": {"kind": "TASK", "title_contains": "Подготовиться", "effort_minutes": 60}},
    {"id": "manual-task-to-event", "input": "Подготовить доклад завтра до 18:00, займёт час", "switch": "EVENT",
     "oracle": {"kind": "EVENT", "manual_kind_wins": True}},
    {"id": "manual-event-to-reminder", "input": "Завтра в 20:00 созвон с Димой на полчаса", "switch": "REMINDER",
     "oracle": {"kind": "REMINDER", "manual_kind_wins": True}},
    {"id": "edit-and-reparse", "input": "Завтра встреча с Димой в 18:00 на час", "replace": "Напомни завтра в 09:00 купить молоко",
     "oracle": {"kind": "REMINDER", "title": "Купить молоко", "new_interpretation": True}},
    {"id": "stale-field-clearing", "input": "Завтра встреча с Димой в 18:00 на час", "replace": "Купить молоко, займёт 15 минут, без дедлайна",
     "oracle": {"kind": "TASK", "title": "Купить молоко", "no_old_event_fields": True}},
    {"id": "title-integrity", "input": "Приём у врача завтра в 10:00 на полчаса, напомни за 50 минут до начала",
     "oracle": {"kind": "EVENT", "title": "Приём у врача", "forbidden_title_fragments": ["напомни", "за до начала"]}},
]


class CaptureAdversarialProbe(OfflineEndToEndTest):
    def _persisted(self, operation: dict) -> dict | None:
        entity_id = operation["entity_id"]
        kind = operation["type"].split(".", 1)[0]
        if kind in {"task", "event"}:
            rows = self._db(
                "SELECT o.kind,o.title,o.category,t.estimated_total_effort_minutes,t.cutoff_state,t.actual_cutoff_at,"
                "rs.remind_at,e.starts_at,e.ends_at,er.lead_minutes FROM obligations o "
                "LEFT JOIN tasks t ON t.obligation_id=o.id LEFT JOIN events e ON e.obligation_id=o.id "
                "LEFT JOIN reminder_states rs ON rs.account_id=o.account_id AND rs.task_id=o.id "
                "LEFT JOIN event_reminders er ON er.event_id=o.id WHERE o.account_id=? AND o.id=?",
                ACCOUNT, entity_id,
            )
        elif kind == "reminder":
            rows = self._db("SELECT title,remind_at,delivery,wake_check,raise_volume,status FROM reminders WHERE account_id=? AND id=?", ACCOUNT, entity_id)
        elif kind == "note":
            rows = self._db("SELECT content,source_kind,lifecycle_status FROM notes WHERE account_id=? AND id=?", ACCOUNT, entity_id)
        else:
            return None
        return dict(rows[0]) if rows else None

    @staticmethod
    def _preview(sheet) -> dict:
        card = sheet.locator(".capture-card")
        card.wait_for()
        kind_chip = sheet.locator('[data-chip-group="capture-kind"] .on')
        result = {
            "kind": card.get_attribute("data-kind") or (kind_chip.get_attribute("data-value") if kind_chip.count() else "TASK"),
            "selected_kind_chip": kind_chip.get_attribute("data-value") if kind_chip.count() else None,
            "title": card.locator(".capture-title").inner_text() if card.locator(".capture-title").count() else None,
            "text": card.inner_text(),
            "create_disabled": sheet.locator("[data-create]").is_disabled(),
        }
        if card.locator('[data-chip-group="card-lead"] [data-value]').count():
            result["available_leads"] = card.locator('[data-chip-group="card-lead"] [data-value]').evaluate_all("els => els.map(e => e.dataset.value)")
            selected = card.locator('[data-chip-group="card-lead"] .on')
            result["selected_lead"] = selected.get_attribute("data-value") if selected.count() else None
        return result

    def test_representative_preview_create_api_and_persistence(self) -> None:
        page = self._page()
        self._ready(page, "today")
        evidence = []
        for scenario in SCENARIOS:
            before = sum(len(request["operations"]) for request in self.sync_requests)
            page.locator(".fab").click()
            sheet = page.locator("dialog.sheet[open]")
            input_ = sheet.locator("#capture-text")
            input_.fill(scenario["input"])
            first_preview = self._preview(sheet)
            if scenario.get("replace"):
                input_.fill(scenario["replace"])
                page.wait_for_timeout(350)
            if scenario.get("switch"):
                sheet.locator(f'[data-chip-group="capture-kind"] [data-value="{scenario["switch"]}"]').click()
                page.wait_for_timeout(100)
            final_preview = self._preview(sheet)
            self.assertFalse(sheet.locator("[data-create]").is_disabled(), scenario["id"])
            sheet.locator("[data-create]").click()
            sheet.wait_for(state="detached")
            self._wait_synced(page)
            sent = [op for request in self.sync_requests for op in request["operations"]]
            self.assertGreater(len(sent), before, scenario["id"])
            operation = sent[before]
            evidence.append({
                "id": scenario["id"], "input": scenario["input"], "replacement_input": scenario.get("replace"),
                "manual_switch": scenario.get("switch"), "semantic_oracle": scenario["oracle"],
                "initial_preview": first_preview, "final_preview": final_preview,
                "outgoing_sync_operation": operation, "persisted_entity": self._persisted(operation),
            })
        payload = {
            "method": "real rendered mobile viewport -> capture preview -> Create -> /api/v1/sync -> SQLite persistence",
            "viewport": {"width": 390, "height": 844}, "scenario_count": len(evidence),
            "scenarios": evidence, "page_errors": self.errors,
        }
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.assertEqual(self.errors, [])
        page.close()


if __name__ == "__main__":
    import unittest
    unittest.main()
