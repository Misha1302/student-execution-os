package io.github.misha1302.seos.reminders;

import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.List;
import java.util.Locale;
import java.util.TimeZone;

/**
 * Turns a notification button into the server's offline-sync operations.
 *
 * <p>Pure logic (no Android types) so it is unit-tested on the JVM. The operation id
 * is derived from the reminder id and the button, so a double tap, a WorkManager
 * retry or a replay after a lost response is applied exactly once by /api/v1/sync.
 */
public final class ReminderActions {
    public static final String START = "START";
    public static final String DONE = "DONE";
    public static final String SNOOZE_30 = "SNOOZE_30";
    public static final String SNOOZE_60 = "SNOOZE_60";

    private ReminderActions() {}

    /** Buttons the device executes itself without opening the app. */
    public static boolean runsInBackground(String action) {
        return START.equals(action) || DONE.equals(action) || snoozeMinutes(action) > 0;
    }

    public static int snoozeMinutes(String action) {
        if (SNOOZE_30.equals(action)) return 30;
        if (SNOOZE_60.equals(action)) return 60;
        return 0;
    }

    /** The snooze moment is fixed when the button is pressed, not when the network returns. */
    public static long snoozeUntil(String action, long pressedAtMillis) {
        return (pressedAtMillis / 60_000L) * 60_000L + snoozeMinutes(action) * 60_000L;
    }

    /** ISO-8601 UTC instant ("2026-09-25T15:00:00Z"), as the sync API expects. */
    public static String iso(long millis) {
        SimpleDateFormat format = new SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss'Z'", Locale.ROOT);
        format.setTimeZone(TimeZone.getTimeZone("UTC"));
        return format.format(new Date(millis));
    }

    /** JSON array of sync operations, one per task the reminder is about. */
    public static String operations(String action, String messageId, List<String> taskIds, long pressedAtMillis) {
        List<String> ops = new ArrayList<>();
        for (int i = 0; i < taskIds.size(); i++) {
            String taskId = taskIds.get(i);
            String opId = "push-" + messageId + "-" + action + (taskIds.size() > 1 ? "-" + i : "");
            String type;
            String payload;
            if (START.equals(action)) {
                type = "task.start";
                payload = "{\"reminder_message_id\":" + quote(messageId) + "}";
            } else if (DONE.equals(action)) {
                type = "task.complete";
                payload = "{\"reminder_message_id\":" + quote(messageId) + "}";
            } else if (snoozeMinutes(action) > 0) {
                type = "reminder.snooze";
                payload = "{\"until\":" + quote(iso(snoozeUntil(action, pressedAtMillis)))
                        + ",\"reminder_message_id\":" + quote(messageId) + "}";
            } else {
                throw new IllegalArgumentException("not a background action: " + action);
            }
            ops.add("{\"op_id\":" + quote(opId) + ",\"type\":" + quote(type) + ",\"entity_id\":" + quote(taskId)
                    + ",\"payload\":" + payload + "}");
        }
        StringBuilder out = new StringBuilder("[");  // String.join needs API 26; minSdk is 24
        for (int i = 0; i < ops.size(); i++) out.append(i == 0 ? "" : ",").append(ops.get(i));
        return out.append(']').toString();
    }

    /** Fills "{title}"/"{time}" placeholders of a server-provided label. */
    public static String fill(String template, String title, String time) {
        if (template == null) return "";
        return template.replace("{title}", title == null ? "" : title).replace("{time}", time == null ? "" : time);
    }

    static String quote(String value) {
        StringBuilder out = new StringBuilder("\"");
        for (char c : value.toCharArray()) {
            switch (c) {
                case '"': out.append("\\\""); break;
                case '\\': out.append("\\\\"); break;
                case '\n': out.append("\\n"); break;
                case '\r': out.append("\\r"); break;
                case '\t': out.append("\\t"); break;
                default:
                    if (c < 0x20) out.append(String.format(Locale.ROOT, "\\u%04x", (int) c));
                    else out.append(c);
            }
        }
        return out.append('"').toString();
    }
}
