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
        JSONObject start = new JSONArray(ReminderActions.operations("START", "rem-2", Collections.singletonList("t"), PRESSED)).getJSONObject(0);
        JSONObject done = new JSONArray(ReminderActions.operations("DONE", "rem-2", Collections.singletonList("t"), PRESSED + 5000)).getJSONObject(0);
        assertEquals("task.start", start.getString("type"));
        assertEquals("task.complete", done.getString("type"));
        assertEquals("push-rem-2-DONE", done.getString("op_id"));
        // The same button on the same reminder always produces the same operation.
        assertEquals(ReminderActions.operations("DONE", "rem-2", Collections.singletonList("t"), PRESSED),
                ReminderActions.operations("DONE", "rem-2", Collections.singletonList("t"), PRESSED + 5000));
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
}
