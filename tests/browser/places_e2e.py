"""Places end to end: Chromium against a real server and database.

Scenario 4: the user creates Дом and ВШЭ, says where they are, records the travel time,
puts an event at ВШЭ — Today shows when to leave, and the planner used the route.
Scenario 5 (server side of it): «Когда приду домой, напомни разобрать вещи» becomes a
typed ENTER trigger on Дом; «когда буду у магазина…» lets the user create the place.
Native geofence delivery is Android's (see mobile tests); a browser only manages them.
"""
from __future__ import annotations

import json
import unittest

from tests.browser.harness import ACCOUNT, RealServerTestCase


class PlacesEndToEndTest(RealServerTestCase):
    def _new_place(self, page, name: str, alias: str = "", lat: str = "", lon: str = "") -> None:
        page.locator('[data-action="place-new"]').first.click()
        sheet = page.locator("dialog.sheet[open]")
        sheet.locator("[data-place-name]").fill(name)
        if alias:
            sheet.locator("[data-place-alias]").fill(alias)
        # Raw coordinates and access settings are folded away from the everyday form.
        self.assertFalse(sheet.locator("[data-place-lat]").is_visible())
        self.assertFalse(sheet.locator("[data-place-routing]").is_visible())
        self.assertIn("Точки пока нет", sheet.locator("[data-place-position-state]").inner_text())
        if lat:
            sheet.locator("[data-place-advanced] summary").click()
            sheet.locator("[data-place-lat]").fill(lat)
            sheet.locator("[data-place-lon]").fill(lon)
            self.assertIn("Точка сохранена", sheet.locator("[data-place-position-state]").inner_text())
        sheet.locator("[data-place-save]").click()
        page.locator("dialog.sheet[open]").wait_for(state="detached")

    def test_scenario_4_places_route_and_event_travel(self):
        page = self._page("#/places")
        self._ready(page, "places")
        self._new_place(page, "Дом", lat="55.7512", lon="37.6184")
        self._new_place(page, "Высшая школа экономики", alias="ВШЭ", lat="55.7539", lon="37.6488")
        self._wait_db(page, "SELECT * FROM places WHERE account_id=?", ACCOUNT, want=lambda rows: len(rows) == 2)
        # The list never carries exact data, even though the user typed it.
        listing = page.evaluate("JSON.stringify(Object.entries(localStorage).filter(([k]) => k.startsWith('seos.cache.')))")
        self.assertNotIn("55.7512", listing)
        page.evaluate("location.reload()")
        self._ready(page, "places")
        self._no_overflow(page)
        # «Где я сейчас»: Дом, for 3 hours.
        page.locator('[data-action="where-am-i"]').click()
        sheet = page.locator("dialog.sheet[open]")
        sheet.locator('[data-chip-group="where-place"] .chip-toggle', has_text="Дом").click()
        sheet.locator("[data-where-save]").click()
        self._wait_db(page, "SELECT * FROM current_location_context WHERE account_id=? AND state='KNOWN'", ACCOUNT)
        # Travel time Дом → ВШЭ: 35 usually, 45 with margin.
        page.locator('[data-action="route-new"]').click()
        sheet = page.locator("dialog.sheet[open]")
        sheet.locator('[data-chip-group="route-from"] .chip-toggle', has_text="Дом").click()
        sheet.locator('[data-chip-group="route-to"] .chip-toggle', has_text="ВШЭ").click()
        sheet.locator("[data-route-expected]").fill("35")
        sheet.locator("[data-route-safe]").fill("45")
        sheet.locator("[data-route-save]").click()
        self._wait_db(page, "SELECT * FROM travel_estimates WHERE account_id=? AND source='USER_OVERRIDE'", ACCOUNT)
        # An event at ВШЭ, chosen in the event form.
        page.evaluate("location.reload()")
        self._ready(page, "places")
        self._go(page, "calendar")
        page.evaluate("import('/assets/js/events.js').then((m) => m.newEventSheet({ starts_at: new Date('2026-10-06T09:00:00Z').toISOString() }))")
        sheet = page.locator("dialog.sheet[open]")
        sheet.locator('[data-e="title"]').fill("Семинар по матану")
        sheet.locator('[data-e="place"]').select_option(label="ВШЭ")
        sheet.locator("[data-save]").click()
        events = self._wait_db(page, "SELECT e.location_effect_kind,e.destination_place_id FROM events e "
                                     "JOIN obligations o ON o.id=e.obligation_id WHERE o.title='Семинар по матану'")
        self.assertEqual(events[0]["location_effect_kind"], "STAY")
        self._go(page, "today")
        card = page.locator(".travel-card")
        card.wait_for()
        text = card.inner_text()
        self.assertIn("Дом", text)
        self.assertIn("ВШЭ", text)
        self.assertIn("11:15", text)  # 12:00 − 45 min with margin
        self._no_overflow(page)
        self.assertEqual(self.errors, [])

    def test_scenario_5_place_trigger_from_words_and_new_place_from_the_card(self):
        page = self._page("#/places")
        self._ready(page, "places")
        if not self._db("SELECT 1 FROM places WHERE account_id=? AND display_name='Дом'", ACCOUNT):
            self._new_place(page, "Дом", lat="55.7512", lon="37.6184")
            self._wait_db(page, "SELECT * FROM places WHERE account_id=? AND display_name='Дом'", ACCOUNT)
        page.evaluate("location.reload()")
        self._ready(page, "places")
        sheet = self._say(page, "Когда приду домой, напомни разобрать вещи")
        self.assertIn("когда приду: Дом", sheet.locator(".command-card").inner_text())
        sheet.locator("[data-run]").click()
        page.locator("dialog.sheet[open]").wait_for(state="detached")
        triggers = self._wait_db(page, "SELECT * FROM location_triggers WHERE title='Разобрать вещи'")
        self.assertEqual((triggers[0]["transition"], triggers[0]["status"]), ("ENTER", "ARMED"))
        # A place the user has not saved yet: they create it from the card.
        sheet = self._say(page, "Когда буду у магазина, напомни купить молоко")
        card = sheet.locator(".command-card")
        self.assertTrue(card.locator("[data-run]").is_disabled())
        card.locator("[data-place-pick]", has_text="магазина").click()
        card.locator("[data-run]").click()
        page.locator("dialog.sheet[open]").wait_for(state="detached")
        self._wait_db(page, "SELECT * FROM location_triggers WHERE title='Купить молоко'")
        self._wait_db(page, "SELECT * FROM places WHERE display_name='Магазина'")
        self._go(page, "places")
        page.locator(".row", has_text="Купить молоко").wait_for()
        self.assertIn("Напомнить, когда приду: Дом", page.locator(".row", has_text="Разобрать вещи").inner_text())
        self.assertIn("нет точки на карте", page.locator(".row", has_text="Купить молоко").inner_text())
        armed = json.loads(page.evaluate("fetch('/api/v1/location-triggers/armed').then((r) => r.text())"))
        self.assertEqual([t["title"] for t in armed["triggers"]], ["Разобрать вещи"])
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
