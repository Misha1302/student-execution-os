from __future__ import annotations

import json
from pathlib import Path

from tests.browser.offline_e2e import ACCOUNT, OfflineEndToEndTest

OUT = Path("artifacts/capture-adversarial/ui-api-e2e.json")


class CaptureAdversarialProbe(OfflineEndToEndTest):
    def test_known_seed_preview_create_api_and_persistence(self) -> None:
        phrase = "созвон с ариадной в 18:00 завтра на пол часа.\nНапомни за 50 минут до начала"
        page = self._page()
        self._ready(page, "today")
        page.locator(".fab").click()
        sheet = page.locator("dialog.sheet[open]")
        sheet.locator("#capture-text").fill(phrase)
        card = sheet.locator(".capture-card")
        card.wait_for()

        preview = {
            "kind": card.get_attribute("data-kind") or "TASK",
            "title": card.locator(".capture-title").inner_text(),
            "text": card.inner_text(),
            "selected_kind_chip": sheet.locator('[data-chip-group="capture-kind"] .on').get_attribute("data-value"),
            "create_disabled": sheet.locator("[data-create]").is_disabled(),
        }
        if preview["kind"] == "EVENT":
            preview["selected_lead"] = card.locator('[data-chip-group="card-lead"] .on').get_attribute("data-value")
            preview["available_leads"] = card.locator('[data-chip-group="card-lead"] [data-value]').evaluate_all(
                "(els) => els.map((e) => e.dataset.value)"
            )
            preview["when"] = card.locator("[data-event-when]").inner_text()
            sheet.locator("[data-more] summary").click()
            preview["description"] = sheet.locator('[data-event-details] [data-e="description"]').input_value()
            preview["detail_lead"] = sheet.locator('[data-event-details] [data-chip-group="e-lead"] .on').get_attribute("data-value")

        sheet.locator("[data-create]").click()
        sheet.wait_for(state="detached")
        self._wait_synced(page)
        self.assertTrue(self.sync_requests, "capture never reached /api/v1/sync")
        operation = self.sync_requests[-1]["operations"][0]

        rows = self._db(
            "SELECT o.kind,o.title,e.starts_at,e.ends_at,er.lead_minutes "
            "FROM obligations o LEFT JOIN events e ON e.obligation_id=o.id "
            "LEFT JOIN event_reminders er ON er.event_id=o.id "
            "WHERE o.account_id=? AND o.id=?",
            ACCOUNT,
            operation["entity_id"],
        )
        self.assertEqual(len(rows), 1)
        row = rows[0]
        persisted = {
            "kind": row["kind"],
            "title": row["title"],
            "starts_at": row["starts_at"],
            "ends_at": row["ends_at"],
            "lead_minutes": row["lead_minutes"],
        }

        payload = {
            "phrase": phrase,
            "preview": preview,
            "outgoing_sync_operation": operation,
            "persisted_entity": persisted,
            "page_errors": self.errors,
            "semantic_oracle": {
                "kind": "EVENT",
                "title": "Созвон с Ариадной",
                "starts_at_local": "2026-09-30T18:00:00+03:00",
                "duration_minutes": 30,
                "remind_before_minutes": 50,
                "deadline": None,
            },
        }
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.assertEqual(self.errors, [])
        page.close()


if __name__ == "__main__":
    import unittest
    unittest.main()
