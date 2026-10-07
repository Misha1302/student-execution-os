package io.github.misha1302.seos.alarm;

import io.github.misha1302.seos.reminders.ReminderActions;

/**
 * The server's sync operations for what the user did with an alarm. Pure logic (JVM
 * tests). Operation ids are derived from the reminder, its moment and the answer, so
 * a WorkManager retry or a double tap is applied exactly once by /api/v1/sync.
 */
public final class AlarmOps {
    private AlarmOps() {}

    private static String op(String opId, String type, String reminderId, String payload) {
        return "[{\"op_id\":" + ReminderActions.quote(opId) + ",\"type\":" + ReminderActions.quote(type)
                + ",\"entity_id\":" + ReminderActions.quote(reminderId) + ",\"payload\":" + payload + "}]";
    }

    private static String base(AlarmState state) {
        return "alarm-" + state.id + "-" + state.at;
    }

    /** «Я встал» for a wake alarm; «Выключить» closes an ordinary alarm. */
    public static String up(AlarmState state) {
        if (!state.wakeCheck) return op(base(state) + "-DONE", "reminder.done", state.id, "{}");
        return op(base(state) + "-UP", "reminder.ack", state.id, "{\"stage\":\"UP\"}");
    }

    /**
     * «Принял»/«Сделал» or «Не принял»/«Пропущу» on a check-in alarm: the outcome of exactly
     * that occurrence, with the moment of the press. The op id is fixed by the alarm and
     * the answer, so a retry or a double tap is one outcome on the server.
     */
    public static String checkin(AlarmState state, boolean done, long pressedAtMillis) {
        String payload = "{\"template_id\":" + ReminderActions.quote(state.checkinTemplate)
                + ",\"original_recurrence_id\":" + ReminderActions.quote(state.checkinRecurrence)
                + (done ? ",\"occurred_at\":" + ReminderActions.quote(Iso.format(pressedAtMillis)) : "") + "}";
        return op(base(state) + (done ? "-CHECKIN_DONE" : "-CHECKIN_SKIP"),
                done ? "checkin.occurrence.done" : "checkin.occurrence.skip", state.checkinTemplate, payload);
    }

    /** «Не сплю» after the awake check. */
    public static String awake(AlarmState state) {
        return op(base(state) + "-AWAKE", "reminder.ack", state.id, "{\"stage\":\"AWAKE\"}");
    }

    /** «Отложить»: the new moment is fixed when the button is pressed. */
    public static String snooze(AlarmState state, long untilMillis) {
        long until = (untilMillis / 60_000L) * 60_000L;
        return op(base(state) + "-SNOOZE-" + until, "reminder.snooze", state.id,
                "{\"until\":" + ReminderActions.quote(Iso.format(until)) + "}");
    }
}
