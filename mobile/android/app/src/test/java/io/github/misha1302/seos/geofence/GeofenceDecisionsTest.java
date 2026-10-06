package io.github.misha1302.seos.geofence;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNotEquals;
import static org.junit.Assert.assertTrue;

import org.json.JSONArray;
import org.json.JSONObject;
import org.junit.Test;

public class GeofenceDecisionsTest {
    private static final long NOW = 1791298800000L; // 2026-10-06T15:00:00Z
    private static final GeofenceSpec HOME = new GeofenceSpec("trigger-home", "Разобрать вещи", "ENTER",
            55.7512, 37.6184, 150f, false, "Дом");
    private static final GeofenceSpec GYM = new GeofenceSpec("trigger-gym", "Сдать пропуск", "ENTER",
            55.75, 37.6, 150f, true, "Зал");

    @Test
    public void onlyTheTriggersOwnTransitionFires() {
        assertTrue(GeofenceDecisions.shouldFire(HOME, true, 0, NOW));
        assertFalse(GeofenceDecisions.shouldFire(HOME, false, 0, NOW));
        GeofenceSpec leaving = new GeofenceSpec("trigger-hse", "Написать Саше", "EXIT", 55.7, 37.6, 150f, false, "ВШЭ");
        assertTrue(GeofenceDecisions.shouldFire(leaving, false, 0, NOW));
        assertFalse(GeofenceDecisions.shouldFire(leaving, true, 0, NOW));
    }

    @Test
    public void duplicateCallbacksDoNotFireAgain() {
        // One-shot: once fired on this phone, never again.
        assertFalse(GeofenceDecisions.shouldFire(HOME, true, NOW - 1000, NOW));
        assertFalse(GeofenceDecisions.shouldFire(HOME, true, NOW - 86_400_000L, NOW));
        // Repeating: GPS jitter at the door is not a second arrival; the next day is.
        assertFalse(GeofenceDecisions.shouldFire(GYM, true, NOW - 5 * 60_000L, NOW));
        assertTrue(GeofenceDecisions.shouldFire(GYM, true, NOW - GeofenceDecisions.REFIRE_COOLDOWN_MS, NOW));
    }

    @Test
    public void firingOperationIdCollapsesDoubledCallbacks() throws Exception {
        String first = GeofenceDecisions.fireOperations("trigger-home", "ENTER", NOW);
        String doubled = GeofenceDecisions.fireOperations("trigger-home", "ENTER", NOW + 20_000L);
        JSONObject op = new JSONArray(first).getJSONObject(0);
        assertEquals("location_trigger.fire", op.getString("type"));
        assertEquals("trigger-home", op.getString("entity_id"));
        assertEquals("ENTER", op.getJSONObject("payload").getString("transition"));
        assertEquals("2026-10-06T15:00:00Z", op.getJSONObject("payload").getString("occurred_at"));
        assertEquals(op.getString("op_id"), new JSONArray(doubled).getJSONObject(0).getString("op_id"));
        String nextDay = GeofenceDecisions.fireOperations("trigger-home", "ENTER", NOW + 86_400_000L);
        assertNotEquals(op.getString("op_id"), new JSONArray(nextDay).getJSONObject(0).getString("op_id"));
        JSONObject done = new JSONArray(GeofenceDecisions.doneOperations("trigger-home")).getJSONObject(0);
        assertEquals("location_trigger.done", done.getString("type"));
    }

    @Test
    public void specsRoundTripAndRejectNonsense() throws Exception {
        GeofenceSpec parsed = GeofenceSpec.fromJson(HOME.toJson());
        assertTrue(parsed.sameArea(HOME));
        assertEquals("Дом", parsed.placeName);
        GeofenceSpec moved = new GeofenceSpec("trigger-home", "Разобрать вещи", "ENTER", 55.76, 37.6184, 150f, false, "Дом");
        assertFalse(moved.sameArea(HOME));
        try {
            new GeofenceSpec("x", "t", "NEAR", 0, 0, 100f, false, "");
            throw new AssertionError("bad transition accepted");
        } catch (IllegalArgumentException expected) { /* ok */ }
        try {
            new GeofenceSpec("x", "t", "ENTER", 91, 0, 100f, false, "");
            throw new AssertionError("bad position accepted");
        } catch (IllegalArgumentException expected) { /* ok */ }
        assertEquals(50f, new GeofenceSpec("x", "t", "ENTER", 0, 0, 1f, false, "").radiusMeters, 0.0f);
    }
}
