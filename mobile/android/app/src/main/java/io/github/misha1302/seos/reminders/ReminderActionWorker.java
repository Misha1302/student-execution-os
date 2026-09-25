package io.github.misha1302.seos.reminders;

import android.content.Context;
import android.content.SharedPreferences;
import androidx.annotation.NonNull;
import androidx.core.app.NotificationCompat;
import androidx.core.app.NotificationManagerCompat;
import androidx.work.Worker;
import androidx.work.WorkerParameters;
import io.github.misha1302.seos.R;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

/**
 * Sends the operations of one notification button to {@code POST /api/v1/sync}.
 *
 * <p>The server address and session token are read at send time from the app's
 * Capacitor Preferences store (the same values the web client uses), so nothing
 * secret is copied into the work queue, and a sign-out stops pending actions.
 */
public class ReminderActionWorker extends Worker {
    static final String KEY_OPERATIONS = "operations";
    static final String KEY_TAG = "tag";
    static final String KEY_FAILED_LABEL = "failed_label";
    static final String KEY_DEEP_LINK = "deep_link";
    static final String PREFERENCES = "CapacitorStorage";
    static final int MAX_ATTEMPTS = 12;

    public ReminderActionWorker(@NonNull Context context, @NonNull WorkerParameters params) {
        super(context, params);
    }

    @NonNull
    @Override
    public Result doWork() {
        SharedPreferences prefs = getApplicationContext().getSharedPreferences(PREFERENCES, Context.MODE_PRIVATE);
        String server = prefs.getString("seos.server", null);
        String token = prefs.getString("seos.token", null);
        String operations = getInputData().getString(KEY_OPERATIONS);
        if (server == null || server.isEmpty() || token == null || token.isEmpty() || operations == null) {
            return failed();  // signed out on this device: the action cannot be attributed
        }
        try {
            int status = post(server.replaceAll("/+$", "") + "/api/v1/sync", token, "{\"operations\":" + operations + "}");
            if (status == 200) return Result.success();
            if (status == 401 || status == 403) return failed();
            if (status >= 400 && status < 500 && status != 408 && status != 429) return failed();
        } catch (IOException | JSONException network) {
            // Offline, DNS, TLS or a proxy page: WorkManager retries with backoff.
        }
        return getRunAttemptCount() + 1 >= MAX_ATTEMPTS ? failed() : Result.retry();
    }

    private int post(String url, String token, String body) throws IOException, JSONException {
        HttpURLConnection connection = (HttpURLConnection) new URL(url).openConnection();
        try {
            connection.setRequestMethod("POST");
            connection.setConnectTimeout(15_000);
            connection.setReadTimeout(20_000);
            connection.setDoOutput(true);
            connection.setRequestProperty("Content-Type", "application/json");
            connection.setRequestProperty("Accept", "application/json");
            connection.setRequestProperty("Authorization", "Bearer " + token);
            byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
            connection.setFixedLengthStreamingMode(bytes.length);
            try (OutputStream out = connection.getOutputStream()) {
                out.write(bytes);
            }
            int status = connection.getResponseCode();
            if (status == 200) {
                // Parse to be sure this is the sync API and not, say, a captive portal page.
                JSONArray results = new JSONObject(read(connection.getInputStream())).getJSONArray("results");
                for (int i = 0; i < results.length(); i++) {
                    String state = results.getJSONObject(i).optString("status");
                    if ("REJECTED".equals(state)) return 422;  // nothing to retry; tell the user
                }
            }
            return status;
        } finally {
            connection.disconnect();
        }
    }

    private static String read(InputStream stream) throws IOException {
        try (InputStream in = stream; ByteArrayOutputStream out = new ByteArrayOutputStream()) {
            byte[] buffer = new byte[8192];
            for (int n; (n = in.read(buffer)) > 0; ) out.write(buffer, 0, n);
            return out.toString("UTF-8");
        }
    }

    private Result failed() {
        String label = getInputData().getString(KEY_FAILED_LABEL);
        String tag = getInputData().getString(KEY_TAG);
        String deepLink = getInputData().getString(KEY_DEEP_LINK);
        if (label != null && !label.isEmpty() && tag != null) {
            Context context = getApplicationContext();
            ReminderNotifications.ensureChannel(context);
            NotificationCompat.Builder builder = new NotificationCompat.Builder(context, ReminderNotifications.CHANNEL)
                    .setSmallIcon(R.drawable.ic_stat_reminder)
                    .setContentTitle(label)
                    .setAutoCancel(true)
                    .setContentIntent(ReminderNotifications.openApp(context, deepLink == null ? "/today" : deepLink, 1));
            try {
                NotificationManagerCompat.from(context).notify(tag, ReminderNotifications.NOTIFICATION_ID, builder.build());
            } catch (SecurityException denied) {
                // Notifications are off; the app shows the reminder in its inbox.
            }
        }
        return Result.failure();
    }
}
