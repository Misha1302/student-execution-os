package io.github.misha1302.seos.reminders;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertTrue;

import android.app.Notification;
import android.app.NotificationManager;
import android.content.Context;
import android.os.Bundle;
import android.os.ParcelFileDescriptor;
import android.service.notification.StatusBarNotification;
import android.util.Base64;
import androidx.test.core.app.ApplicationProvider;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import java.io.ByteArrayOutputStream;
import java.io.FileInputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.Iterator;
import java.util.Map;
import org.json.JSONObject;
import org.junit.Assume;
import org.junit.Test;
import org.junit.runner.RunWith;

/**
 * Real device check of the notification buttons against a running server:
 * a reminder rendered exactly as a push would render it, its "Snooze" and "Done"
 * buttons pressed through the system notification, and the result read back from
 * the server's API. Only FCM transport itself is not exercised here.
 *
 * <p>Run with instrumentation arguments seosServer, seosToken and seosReminder
 * (base64 JSON of the push data); see mobile/scripts/native-e2e.sh. Without them
 * the test is skipped.
 */
@RunWith(AndroidJUnit4.class)
public class ReminderNotificationEndToEndTest {

    @Test
    public void snoozeAndDoneFromTheNotificationReachTheServer() throws Exception {
        Bundle args = InstrumentationRegistry.getArguments();
        String server = args.getString("seosServer");
        String token = args.getString("seosToken");
        String encoded = args.getString("seosReminder");
        Assume.assumeTrue("no server arguments", server != null && token != null && encoded != null);

        Context context = ApplicationProvider.getApplicationContext();
        shell("pm grant " + context.getPackageName() + " android.permission.POST_NOTIFICATIONS");
        context.getSharedPreferences(ReminderActionWorker.PREFERENCES, Context.MODE_PRIVATE).edit()
                .putString("seos.server", server).putString("seos.token", token).commit();

        Map<String, String> data = new HashMap<>();
        JSONObject json = new JSONObject(new String(Base64.decode(encoded, Base64.DEFAULT), StandardCharsets.UTF_8));
        for (Iterator<String> keys = json.keys(); keys.hasNext(); ) {
            String key = keys.next();
            data.put(key, json.get(key).toString());
        }
        ReminderNotifications.Reminder reminder = ReminderNotifications.parse(data);
        String taskId = reminder.taskIds.get(0);

        ReminderNotifications.show(context, reminder);
        Notification shown = find(context, reminder.tag);
        assertNotNull("reminder notification is shown", shown);
        assertEquals(reminder.title, shown.extras.getString(Notification.EXTRA_TITLE));
        assertEquals(3, shown.actions.length);

        // Snooze: the server schedules the next reminder for the chosen moment.
        press(shown, "SNOOZE", reminder);
        JSONObject snoozed = waitFor(server, token, taskId, "remind_at");
        assertTrue("server scheduled the next reminder", !snoozed.isNull("remind_at"));

        // The reminder comes again; Start turns it into an "In progress" notification …
        ReminderNotifications.show(context, reminder);
        press(find(context, reminder.tag), "START", reminder);
        JSONObject started = waitFor(server, token, taskId, "started_at");
        assertTrue("task started on the server", !started.isNull("started_at"));
        Notification inProgress = find(context, reminder.tag);
        assertNotNull(inProgress);
        assertEquals(1, inProgress.actions.length);
        assertEquals(reminder.label("done"), inProgress.actions[0].title.toString());

        // … whose Done completes the task.
        inProgress.actions[0].actionIntent.send();
        JSONObject done = waitFor(server, token, taskId, "COMPLETED");
        assertEquals("COMPLETED", done.getString("status"));
    }

    private static void press(Notification notification, String action, ReminderNotifications.Reminder reminder) throws Exception {
        for (int i = 0; i < reminder.actions.length(); i++) {
            String id = reminder.actions.getJSONObject(i).getString("id");
            if (id.startsWith(action)) {
                notification.actions[i].actionIntent.send();
                return;
            }
        }
        throw new AssertionError("no " + action + " button on the reminder");
    }

    private static Notification find(Context context, String tag) throws InterruptedException {
        NotificationManager manager = context.getSystemService(NotificationManager.class);
        for (int attempt = 0; attempt < 20; attempt++) {
            for (StatusBarNotification item : manager.getActiveNotifications()) {
                if (tag.equals(item.getTag()) && item.getNotification().actions != null
                        && item.getNotification().actions.length > 0) return item.getNotification();
            }
            Thread.sleep(250);
        }
        return null;
    }

    private static JSONObject waitFor(String server, String token, String taskId, String what) throws Exception {
        JSONObject task = null;
        for (int attempt = 0; attempt < 90; attempt++) {
            HttpURLConnection connection = (HttpURLConnection) new URL(server + "/api/v1/tasks/" + taskId).openConnection();
            connection.setRequestProperty("Authorization", "Bearer " + token);
            try (InputStream in = connection.getInputStream()) {
                task = new JSONObject(read(in));
            } finally {
                connection.disconnect();
            }
            boolean reached = what.endsWith("_at") ? !task.isNull(what) : what.equals(task.optString("status"));
            if (reached) return task;
            Thread.sleep(1000);
        }
        return task;
    }

    private static String read(InputStream in) throws Exception {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        byte[] buffer = new byte[8192];
        for (int n; (n = in.read(buffer)) > 0; ) out.write(buffer, 0, n);
        return out.toString("UTF-8");
    }

    private static void shell(String command) throws Exception {
        ParcelFileDescriptor fd = InstrumentationRegistry.getInstrumentation().getUiAutomation().executeShellCommand(command);
        try (InputStream in = new FileInputStream(fd.getFileDescriptor())) {
            read(in);
        }
        fd.close();
    }
}
