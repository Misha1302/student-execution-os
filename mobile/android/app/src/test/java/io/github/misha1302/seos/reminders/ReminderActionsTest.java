package io.github.misha1302.seos.reminders;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import java.util.Arrays;
import java.util.Collections;
import org.json.JSONArray;
import org.json.JSONObject;
import org.junit.Test;

public class ReminderActionsTest {
    // 2026-09-24T16:00:37Z
    private static final long PRESSED = 1790265637000L;

    @Test
    public void snoozeBecomesAnAbsoluteUntilFixedAtPressTime() throws Exception {
        JSONArray ops = new JSONArray(ReminderActions.operations("SNOOZE_60", "rem-1", Collections.singletonList("task-1"), PRESSED));
        assertEquals(1, ops.length());
        JSONObject op = ops.getJSONObject(0);
        assertEquals("push-rem-1-SNOOZE_60", op.getString("op_id"));
        assertEquals("reminder.snooze", op.getString("type"));
        assertEquals("task-1", op.getString("entity_id"));
        assertEquals("2026-09-24T17:00:00Z", op.getJSONObject("payload").getString("until"));
        assertEquals("rem-1", op.getJSONObject("payload").getString("reminder_message_id"));
    }

    @Test
    public void startAndDoneAreLifecycleOperationsWithStableIds() throws Exception {
        String startJson = ReminderActions.operations("START", "rem-2", Collections.singletonList("t"), PRESSED);
        JSONObject start = new JSONArray(startJson).getJSONObject(0);
        JSONObject done = new JSONArray(ReminderActions.operations("DONE", "rem-2", Collections.singletonList("t"), PRESSED + 5000)).getJSONObject(0);
        assertEquals("execution.start", start.getString("type"));
        assertTrue(start.getString("entity_id").startsWith("execution-"));
        assertEquals("t", start.getJSONObject("payload").getString("task_id"));
        assertEquals("2026-09-24T16:00:37Z", start.getJSONObject("payload").getString("occurred_at"));
        assertEquals("rem-2", start.getJSONObject("payload").getString("reminder_message_id"));
        assertEquals(startJson, ReminderActions.operations("START", "rem-2", Collections.singletonList("t"), PRESSED));
        assertEquals("task.complete", done.getString("type"));
        assertEquals("push-rem-2-DONE-" + (PRESSED + 5000), done.getString("op_id"));
        assertEquals("2026-09-24T16:00:42Z", done.getJSONObject("payload").getString("occurred_at"));
        // A WorkManager retry reuses the exact serialized operation generated on the
        // original press, including its id and user-reported occurrence time.
        assertEquals(ReminderActions.operations("DONE", "rem-2", Collections.singletonList("t"), PRESSED),
                ReminderActions.operations("DONE", "rem-2", Collections.singletonList("t"), PRESSED));
    }

    @Test
    public void groupedReminderActsOnEveryTask() throws Exception {
        JSONArray ops = new JSONArray(ReminderActions.operations("SNOOZE_30", "rem-3", Arrays.asList("a", "b"), PRESSED));
        assertEquals(2, ops.length());
        assertEquals("push-rem-3-SNOOZE_30-1", ops.getJSONObject(1).getString("op_id"));
        assertEquals("b", ops.getJSONObject(1).getString("entity_id"));
    }

    @Test
    public void onlyStateChangingButtonsRunInTheBackground() throws Exception {
        assertTrue(ReminderActions.runsInBackground("START"));
        assertTrue(ReminderActions.runsInBackground("SNOOZE_30"));
        assertFalse(ReminderActions.runsInBackground("RESCHEDULE"));
        assertFalse(ReminderActions.runsInBackground("OPEN"));
    }

    @Test
    public void labelsAndIdsAreEscaped() throws Exception {
        assertEquals("Напомню в 16:30", ReminderActions.fill("Напомню в {time}", "x", "16:30"));
        JSONObject op = new JSONArray(ReminderActions.operations("DONE", "r\"1", Collections.singletonList("t\\1"), PRESSED)).getJSONObject(0);
        assertEquals("t\\1", op.getString("entity_id"));
    }

    @Test
    public void standaloneReminderButtonsActOnTheReminder() throws Exception {
        JSONObject done = new JSONArray(ReminderActions.reminderOperations("DONE", "rem-9", "reminder-bread", PRESSED)).getJSONObject(0);
        assertEquals("reminder.done", done.getString("type"));
        assertEquals("reminder-bread", done.getString("entity_id"));
        assertEquals("push-rem-9-DONE", done.getString("op_id"));
        JSONObject later = new JSONArray(ReminderActions.reminderOperations("SNOOZE_10", "rem-9", "reminder-bread", PRESSED)).getJSONObject(0);
        assertEquals("reminder.snooze", later.getString("type"));
        assertEquals("2026-09-24T16:10:00Z", later.getJSONObject("payload").getString("until"));
    }

    @Test
    public void httpOkConflictIsNotTreatedAsApplied() throws Exception {
        JSONArray conflict = new JSONArray().put(new JSONObject().put("status", "CONFLICT").put("code", "TASK_CANCELLED"));
        JSONArray rejected = new JSONArray().put(new JSONObject().put("status", "REJECTED").put("code", "VALIDATION_ERROR"));
        JSONArray applied = new JSONArray().put(new JSONObject().put("status", "APPLIED"));
        JSONArray safeNoop = new JSONArray().put(new JSONObject().put("status", "NOOP").put("code", "ALREADY_COMPLETED"));
        JSONArray unsafeNoop = new JSONArray().put(new JSONObject().put("status", "NOOP").put("code", "UNKNOWN"));
        assertEquals(SyncResultPolicy.Decision.PERMANENT_FAILURE, SyncResultPolicy.decide(conflict));
        assertEquals(SyncResultPolicy.Decision.PERMANENT_FAILURE, SyncResultPolicy.decide(rejected));
        assertEquals(SyncResultPolicy.Decision.SUCCESS, SyncResultPolicy.decide(applied));
        assertEquals(SyncResultPolicy.Decision.SUCCESS, SyncResultPolicy.decide(safeNoop));
        assertEquals(SyncResultPolicy.Decision.PERMANENT_FAILURE, SyncResultPolicy.decide(unsafeNoop));
    }

    @Test
    public void checkinButtonsRecordTheOutcomeOfThatOccurrence() throws Exception {
        JSONObject taken = new JSONArray(ReminderActions.checkinOperations(
                "CHECKIN_DONE", "rem-7", "checkin-vit", "2026-09-24T19:00:00", "checkin-r1", PRESSED)).getJSONObject(0);
        assertEquals("checkin.occurrence.done", taken.getString("type"));
        assertEquals("checkin-vit", taken.getString("entity_id"));
        assertEquals("push-rem-7-CHECKIN_DONE", taken.getString("op_id"));
        JSONObject payload = taken.getJSONObject("payload");
        assertEquals("checkin-vit", payload.getString("template_id"));
        assertEquals("2026-09-24T19:00:00", payload.getString("original_recurrence_id"));
        // The moment of the press, not of the network coming back.
        assertEquals("2026-09-24T16:00:37Z", payload.getString("occurred_at"));
        assertEquals("rem-7", payload.getString("reminder_message_id"));
        // A retry or a double tap is the same operation (exactly once on the server).
        assertEquals(ReminderActions.checkinOperations("CHECKIN_DONE", "rem-7", "checkin-vit", "2026-09-24T19:00:00",
                "checkin-r1", PRESSED), ReminderActions.checkinOperations("CHECKIN_DONE", "rem-7", "checkin-vit",
                "2026-09-24T19:00:00", "checkin-r1", PRESSED));
        JSONObject skipped = new JSONArray(ReminderActions.checkinOperations(
                "CHECKIN_SKIP", "rem-7", "checkin-vit", "2026-09-24T19:00:00", "checkin-r1", PRESSED)).getJSONObject(0);
        assertEquals("checkin.occurrence.skip", skipped.getString("type"));
        assertFalse(skipped.getJSONObject("payload").has("occurred_at"));
    }

    @Test
    public void checkinSnoozeMovesOnlyThePrompt() throws Exception {
        JSONObject later = new JSONArray(ReminderActions.checkinOperations(
                "SNOOZE_15", "rem-7", "checkin-vit", "2026-09-24T19:00:00", "checkin-r1", PRESSED)).getJSONObject(0);
        assertEquals("reminder.snooze", later.getString("type"));
        assertEquals("checkin-r1", later.getString("entity_id"));
        assertEquals("2026-09-24T16:15:00Z", later.getJSONObject("payload").getString("until"));
        assertTrue(ReminderActions.runsInBackground("CHECKIN_DONE"));
        assertTrue(ReminderActions.runsInBackground("CHECKIN_SKIP"));
        assertEquals(15, ReminderActions.snoozeMinutes("SNOOZE_15"));
    }

    @Test
    public void checkinPushCarriesTheOccurrenceIdentity() throws Exception {
        java.util.Map<String, String> data = new java.util.HashMap<>();
        data.put("message_id", "rem-8");
        data.put("reminder_id", "checkin-r1");
        data.put("title", "💊 Витамин D");
        data.put("checkin", "{\"template_id\":\"checkin-vit\",\"original_recurrence_id\":\"2026-09-24T09:00:00\",\"kind\":\"MEDICATION\"}");
        ReminderNotifications.Reminder reminder = ReminderNotifications.parse(data);
        assertTrue(reminder.isCheckin());
        assertEquals("checkin-vit", reminder.checkinTemplateId);
        assertEquals("2026-09-24T09:00:00", reminder.checkinRecurrenceId);
        data.remove("checkin");
        assertFalse(ReminderNotifications.parse(data).isCheckin());
    }

    @Test
    public void alreadySkippedIsASafeReplay() throws Exception {
        JSONArray noop = new JSONArray().put(new JSONObject().put("status", "NOOP").put("code", "ALREADY_SKIPPED"));
        assertEquals(SyncResultPolicy.Decision.SUCCESS, SyncResultPolicy.decide(noop));
    }
}
