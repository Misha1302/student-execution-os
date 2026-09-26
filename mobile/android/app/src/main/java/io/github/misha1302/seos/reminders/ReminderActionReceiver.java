package io.github.misha1302.seos.reminders;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.text.format.DateFormat;
import java.util.Arrays;
import java.util.Date;
import java.util.List;
import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

/**
 * Handles Start / Done / Snooze pressed on a reminder notification.
 *
 * <p>The notification reacts at once; the change itself is a sync operation queued in
 * WorkManager, which survives process death and waits for network. The operation id is
 * fixed by the button and the reminder, so every retry is applied exactly once.
 */
public class ReminderActionReceiver extends BroadcastReceiver {
    static final String EXTRA_ACTION = "seos.action";
    static final String EXTRA_MESSAGE_ID = "seos.message_id";
    static final String EXTRA_TASK_IDS = "seos.task_ids";
    static final String EXTRA_TAG = "seos.tag";
    static final String EXTRA_TASK_TITLE = "seos.task_title";
    static final String EXTRA_LABELS = "seos.labels";
    static final String EXTRA_REMINDER_ID = "seos.reminder_id";

    @Override
    public void onReceive(Context context, Intent intent) {
        String action = intent.getStringExtra(EXTRA_ACTION);
        String messageId = intent.getStringExtra(EXTRA_MESSAGE_ID);
        String[] taskIds = intent.getStringArrayExtra(EXTRA_TASK_IDS);
        String reminderId = intent.getStringExtra(EXTRA_REMINDER_ID);
        if (taskIds == null) taskIds = new String[0];
        boolean standalone = reminderId != null && !reminderId.isEmpty();
        if (action == null || messageId == null || (taskIds.length == 0 && !standalone)) return;
        ReminderNotifications.Reminder reminder = fromIntent(intent, action, messageId, Arrays.asList(taskIds), reminderId);
        long pressedAt = System.currentTimeMillis();
        String operations = standalone
                ? ReminderActions.reminderOperations(action, messageId, reminderId, pressedAt)
                : ReminderActions.operations(action, messageId, reminder.taskIds, pressedAt);
        enqueue(context, "seos-" + messageId + "-" + action, operations, reminder);

        if (ReminderActions.START.equals(action)) {
            ReminderNotifications.showStarted(context, reminder);
        } else if (ReminderActions.snoozeMinutes(action) > 0) {
            long until = ReminderActions.snoozeUntil(action, pressedAt);
            String time = DateFormat.getTimeFormat(context).format(new Date(until));
            ReminderNotifications.showInfo(context, reminder, ReminderActions.fill(reminder.label("snoozed"), reminder.taskTitle, time));
        } else {
            ReminderNotifications.showInfo(context, reminder, ReminderActions.fill(reminder.label("completed"), reminder.taskTitle, null));
        }
    }

    static void enqueue(Context context, String uniqueName, String operations, ReminderNotifications.Reminder reminder) {
        ReminderActionWorker.enqueue(context, uniqueName, operations, reminder.tag, reminder.label("failed"), reminder.deepLink);
    }

    private static ReminderNotifications.Reminder fromIntent(Intent intent, String action, String messageId, List<String> taskIds,
                                                             String reminderId) {
        JSONObject labels;
        try {
            labels = new JSONObject(intent.getStringExtra(EXTRA_LABELS) == null ? "{}" : intent.getStringExtra(EXTRA_LABELS));
        } catch (JSONException malformed) {
            labels = new JSONObject();
        }
        String tag = intent.getStringExtra(EXTRA_TAG);
        String title = intent.getStringExtra(EXTRA_TASK_TITLE);
        String subject = reminderId != null && !reminderId.isEmpty() ? reminderId : taskIds.get(0);
        String link = reminderId != null && !reminderId.isEmpty() ? "/reminder/" + reminderId : "/task/" + subject;
        return new ReminderNotifications.Reminder(messageId, "", "", link, title == null ? "" : title,
                tag == null ? "seos:" + subject : tag, taskIds, new JSONArray(), labels, reminderId);
    }
}
