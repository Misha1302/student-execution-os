package io.github.misha1302.seos.reminders;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.text.format.DateFormat;
import androidx.work.BackoffPolicy;
import androidx.work.Constraints;
import androidx.work.Data;
import androidx.work.ExistingWorkPolicy;
import androidx.work.NetworkType;
import androidx.work.OneTimeWorkRequest;
import androidx.work.WorkManager;
import java.util.Arrays;
import java.util.Date;
import java.util.List;
import java.util.concurrent.TimeUnit;
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

    @Override
    public void onReceive(Context context, Intent intent) {
        String action = intent.getStringExtra(EXTRA_ACTION);
        String messageId = intent.getStringExtra(EXTRA_MESSAGE_ID);
        String[] taskIds = intent.getStringArrayExtra(EXTRA_TASK_IDS);
        if (action == null || messageId == null || taskIds == null || taskIds.length == 0) return;
        ReminderNotifications.Reminder reminder = fromIntent(intent, action, messageId, Arrays.asList(taskIds));
        long pressedAt = System.currentTimeMillis();
        String operations = ReminderActions.operations(action, messageId, reminder.taskIds, pressedAt);
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
        Data input = new Data.Builder()
                .putString(ReminderActionWorker.KEY_OPERATIONS, operations)
                .putString(ReminderActionWorker.KEY_TAG, reminder.tag)
                .putString(ReminderActionWorker.KEY_FAILED_LABEL, reminder.label("failed"))
                .putString(ReminderActionWorker.KEY_DEEP_LINK, reminder.deepLink)
                .build();
        OneTimeWorkRequest request = new OneTimeWorkRequest.Builder(ReminderActionWorker.class)
                .setInputData(input)
                .setConstraints(new Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 15, TimeUnit.SECONDS)
                .addTag("seos-reminder-action")
                .build();
        // KEEP: a second tap on the same button while the first is pending changes nothing.
        WorkManager.getInstance(context).enqueueUniqueWork(uniqueName, ExistingWorkPolicy.KEEP, request);
    }

    private static ReminderNotifications.Reminder fromIntent(Intent intent, String action, String messageId, List<String> taskIds) {
        JSONObject labels;
        try {
            labels = new JSONObject(intent.getStringExtra(EXTRA_LABELS) == null ? "{}" : intent.getStringExtra(EXTRA_LABELS));
        } catch (JSONException malformed) {
            labels = new JSONObject();
        }
        String tag = intent.getStringExtra(EXTRA_TAG);
        String title = intent.getStringExtra(EXTRA_TASK_TITLE);
        return new ReminderNotifications.Reminder(messageId, "", "", "/task/" + taskIds.get(0), title == null ? "" : title,
                tag == null ? "seos:" + taskIds.get(0) : tag, taskIds, new JSONArray(), labels);
    }
}
