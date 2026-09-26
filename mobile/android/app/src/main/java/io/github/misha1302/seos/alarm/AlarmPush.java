package io.github.misha1302.seos.alarm;

import android.content.Context;
import java.util.Map;
import org.json.JSONException;
import org.json.JSONObject;

/** What a push means for this phone's alarms. */
public final class AlarmPush {
    private AlarmPush() {}

    /** "alarm-sync": an alarm was set or changed elsewhere; fetch the schedule. */
    public static void sync(Context context) {
        AlarmSyncWorker.enqueue(context);
    }

    /**
     * A due alarm reminder from the server (the backup path when the local alarm was
     * never scheduled, e.g. set on another device while this phone was off). Rings
     * unless this phone already rang that moment. Returns whether it rang.
     */
    public static boolean handle(Context context, Map<String, String> data) throws JSONException {
        String raw = data.get("alarm");
        if (raw == null || raw.isEmpty()) return false;
        AlarmStore.setLabels(context, new JSONObject(data.getOrDefault("labels", "{}")));
        AlarmState incoming = AlarmState.fromServer(new JSONObject(raw));
        AlarmState known = AlarmStore.get(context, incoming.id);
        if (known != null && known.key().equals(incoming.key()) && !AlarmState.SCHEDULED.equals(known.phase)) {
            return true;  // already rang (or answered) here: the push is a duplicate
        }
        long now = System.currentTimeMillis();
        if (incoming.at < now - AlarmState.STALE_AFTER) return false;
        AlarmScheduler.cancel(context, known != null ? known : incoming);
        AlarmReceiver.ring(context, incoming, now);
        AlarmScheduler.rescheduleAll(context);
        return true;
    }
}
