package io.github.misha1302.seos.geofence;

import io.github.misha1302.seos.reminders.ReminderActions;

/**
 * Pure rules for a platform proximity callback (no Android types, unit-tested on the JVM).
 *
 * <p>The platform may deliver a crossing twice, late, or in the wrong direction for the
 * trigger. Only the trigger's own transition counts; a one-shot trigger fires once on
 * this phone; a repeating one waits {@link #REFIRE_COOLDOWN_MS} (the server applies the
 * same cool-down). The firing operation's id is fixed by the trigger, the transition and
 * a ten-minute bucket of the crossing, so a doubled callback or a WorkManager retry is
 * one operation on the server.
 */
public final class GeofenceDecisions {
    public static final long REFIRE_COOLDOWN_MS = 30L * 60_000L;
    static final long BUCKET_MS = 10L * 60_000L;

    private GeofenceDecisions() {}

    public static boolean shouldFire(GeofenceSpec spec, boolean entering, long lastFiredMillis, long nowMillis) {
        if (spec == null) return false;
        boolean matches = "ENTER".equals(spec.transition) == entering;
        if (!matches) return false;
        if (lastFiredMillis <= 0) return true;
        return spec.repeat && nowMillis - lastFiredMillis >= REFIRE_COOLDOWN_MS;
    }

    public static String fireOperations(String triggerId, String transition, long occurredAtMillis) {
        String opId = "geo-" + triggerId + "-" + transition + "-" + (occurredAtMillis / BUCKET_MS);
        String payload = "{\"transition\":" + ReminderActions.quote(transition)
                + ",\"occurred_at\":" + ReminderActions.quote(ReminderActions.iso(occurredAtMillis)) + "}";
        return "[{\"op_id\":" + ReminderActions.quote(opId) + ",\"type\":\"location_trigger.fire\",\"entity_id\":"
                + ReminderActions.quote(triggerId) + ",\"payload\":" + payload + "}]";
    }

    /** «Готово» on the place notification. */
    public static String doneOperations(String triggerId) {
        return "[{\"op_id\":" + ReminderActions.quote("geo-" + triggerId + "-DONE") + ",\"type\":\"location_trigger.done\","
                + "\"entity_id\":" + ReminderActions.quote(triggerId) + ",\"payload\":{}}]";
    }

    public static int requestCode(String triggerId) {
        return 50_000 + Math.abs(triggerId.hashCode() % 1_000_000);
    }
}
