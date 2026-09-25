package io.github.misha1302.seos.reminders;

import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.net.Uri;
import android.os.Build;
import androidx.core.app.NotificationCompat;
import androidx.core.app.NotificationManagerCompat;
import io.github.misha1302.seos.MainActivity;
import io.github.misha1302.seos.R;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

/**
 * Renders a reminder push as a system notification whose buttons really act.
 *
 * <p>Background buttons (Start, Done, Snooze) are handled by {@link ReminderActionReceiver}
 * without opening the app; Reschedule/Open/Plan open the app at the right screen via a
 * {@code seos://open/...} link that the web client routes.
 */
public final class ReminderNotifications {
    public static final String CHANNEL = "reminders";
    static final int NOTIFICATION_ID = 7001;

    private ReminderNotifications() {}

    /** A parsed reminder push; also rebuilt from the receiver's intent extras. */
    public static final class Reminder {
        public final String messageId;
        public final String title;
        public final String body;
        public final String deepLink;
        public final String taskTitle;
        public final String tag;
        public final List<String> taskIds;
        public final JSONArray actions;
        public final JSONObject labels;

        Reminder(String messageId, String title, String body, String deepLink, String taskTitle, String tag,
                 List<String> taskIds, JSONArray actions, JSONObject labels) {
            this.messageId = messageId;
            this.title = title;
            this.body = body;
            this.deepLink = deepLink;
            this.taskTitle = taskTitle;
            this.tag = tag;
            this.taskIds = taskIds;
            this.actions = actions;
            this.labels = labels;
        }

        public String label(String key) {
            return labels.optString(key, "");
        }
    }

    public static Reminder parse(Map<String, String> data) throws JSONException {
        List<String> taskIds = new ArrayList<>();
        JSONArray ids = new JSONArray(orEmpty(data.get("task_ids"), "[]"));
        for (int i = 0; i < ids.length(); i++) taskIds.add(ids.getString(i));
        String tag = taskIds.size() == 1 ? taskIds.get(0) : "group";
        return new Reminder(orEmpty(data.get("message_id"), ""), orEmpty(data.get("title"), ""), orEmpty(data.get("body"), ""),
                orEmpty(data.get("deep_link"), "/today"), orEmpty(data.get("task_title"), ""), "seos:" + tag, taskIds,
                new JSONArray(orEmpty(data.get("actions"), "[]")), new JSONObject(orEmpty(data.get("labels"), "{}")));
    }

    private static String orEmpty(String value, String fallback) {
        return value == null || value.isEmpty() ? fallback : value;
    }

    static void ensureChannel(Context context) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return;
        NotificationManager manager = context.getSystemService(NotificationManager.class);
        if (manager.getNotificationChannel(CHANNEL) != null) return;
        NotificationChannel channel = new NotificationChannel(CHANNEL, context.getString(R.string.reminder_channel),
                NotificationManager.IMPORTANCE_HIGH);
        channel.setDescription(context.getString(R.string.reminder_channel_description));
        manager.createNotificationChannel(channel);
    }

    /** Opens the app at a client route such as "/task/42?step=reschedule". */
    static PendingIntent openApp(Context context, String route, int requestCode) {
        String path = route.startsWith("/") ? route.substring(1) : route;
        Intent intent = new Intent(Intent.ACTION_VIEW, Uri.parse("seos://open/" + path), context, MainActivity.class);
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        return PendingIntent.getActivity(context, requestCode, intent,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
    }

    static PendingIntent background(Context context, Reminder reminder, String action, int requestCode) {
        Intent intent = new Intent(context, ReminderActionReceiver.class)
                .setAction("io.github.misha1302.seos.REMINDER_" + action)
                .putExtra(ReminderActionReceiver.EXTRA_ACTION, action)
                .putExtra(ReminderActionReceiver.EXTRA_MESSAGE_ID, reminder.messageId)
                .putExtra(ReminderActionReceiver.EXTRA_TASK_IDS, reminder.taskIds.toArray(new String[0]))
                .putExtra(ReminderActionReceiver.EXTRA_TAG, reminder.tag)
                .putExtra(ReminderActionReceiver.EXTRA_TASK_TITLE, reminder.taskTitle)
                .putExtra(ReminderActionReceiver.EXTRA_LABELS, reminder.labels.toString());
        return PendingIntent.getBroadcast(context, requestCode, intent,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
    }

    public static void show(Context context, Reminder reminder) {
        ensureChannel(context);
        int base = Math.abs(reminder.messageId.hashCode() % 100_000) * 10;
        NotificationCompat.Builder builder = new NotificationCompat.Builder(context, CHANNEL)
                .setSmallIcon(R.drawable.ic_stat_reminder)
                .setContentTitle(reminder.title)
                .setContentText(reminder.body)
                .setStyle(new NotificationCompat.BigTextStyle().bigText(reminder.body))
                .setPriority(NotificationCompat.PRIORITY_HIGH)
                .setCategory(NotificationCompat.CATEGORY_REMINDER)
                .setAutoCancel(true)
                .setContentIntent(openApp(context, reminder.deepLink, base));
        String firstTask = reminder.taskIds.isEmpty() ? null : reminder.taskIds.get(0);
        for (int i = 0; i < reminder.actions.length() && i < 3; i++) {
            JSONObject action = reminder.actions.optJSONObject(i);
            if (action == null) continue;
            String id = action.optString("id");
            String label = action.optString("label", id);
            PendingIntent intent;
            if (ReminderActions.runsInBackground(id) && !reminder.taskIds.isEmpty()) {
                intent = background(context, reminder, id, base + 1 + i);
            } else if ("RESCHEDULE".equals(id) && firstTask != null) {
                intent = openApp(context, "/task/" + Uri.encode(firstTask) + "?step=reschedule", base + 1 + i);
            } else if ("REPLAN".equals(id)) {
                intent = openApp(context, "/plan", base + 1 + i);
            } else {
                intent = openApp(context, reminder.deepLink, base + 1 + i);
            }
            builder.addAction(0, label, intent);
        }
        notify(context, reminder.tag, builder);
    }

    /** After Start: the notification turns into "In progress" with a Done button. */
    static void showStarted(Context context, Reminder reminder) {
        ensureChannel(context);
        int base = Math.abs(reminder.messageId.hashCode() % 100_000) * 10;
        NotificationCompat.Builder builder = new NotificationCompat.Builder(context, CHANNEL)
                .setSmallIcon(R.drawable.ic_stat_reminder)
                .setContentTitle(ReminderActions.fill(reminder.label("started"), reminder.taskTitle, null))
                .setContentText(reminder.label("started_body"))
                .setPriority(NotificationCompat.PRIORITY_LOW)
                .setOnlyAlertOnce(true)
                .setSilent(true)
                .setAutoCancel(true)
                .setContentIntent(openApp(context, reminder.deepLink, base))
                .addAction(0, reminder.label("done"), background(context, reminder, ReminderActions.DONE, base + 5));
        notify(context, reminder.tag, builder);
    }

    /** A short, silent confirmation ("I'll remind you at 16:30") that disappears by itself. */
    static void showInfo(Context context, Reminder reminder, String text) {
        ensureChannel(context);
        NotificationCompat.Builder builder = new NotificationCompat.Builder(context, CHANNEL)
                .setSmallIcon(R.drawable.ic_stat_reminder)
                .setContentTitle(text)
                .setPriority(NotificationCompat.PRIORITY_LOW)
                .setSilent(true)
                .setAutoCancel(true)
                .setTimeoutAfter(6000)
                .setContentIntent(openApp(context, reminder.deepLink, Math.abs(reminder.messageId.hashCode() % 100_000) * 10));
        notify(context, reminder.tag, builder);
    }

    static void cancel(Context context, String tag) {
        NotificationManagerCompat.from(context).cancel(tag, NOTIFICATION_ID);
    }

    private static void notify(Context context, String tag, NotificationCompat.Builder builder) {
        NotificationManagerCompat manager = NotificationManagerCompat.from(context);
        if (!manager.areNotificationsEnabled()) return;
        try {
            manager.notify(tag, NOTIFICATION_ID, builder.build());
        } catch (SecurityException denied) {
            // POST_NOTIFICATIONS revoked: nothing can be shown; the in-app inbox still has it.
        }
    }
}
