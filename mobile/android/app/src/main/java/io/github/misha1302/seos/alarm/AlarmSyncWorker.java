package io.github.misha1302.seos.alarm;

import android.content.Context;
import android.content.SharedPreferences;
import androidx.annotation.NonNull;
import androidx.work.Constraints;
import androidx.work.ExistingWorkPolicy;
import androidx.work.NetworkType;
import androidx.work.OneTimeWorkRequest;
import androidx.work.WorkManager;
import androidx.work.Worker;
import androidx.work.WorkerParameters;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.ArrayList;
import java.util.List;
import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

/**
 * Fetches the account's upcoming alarms ({@code GET /api/v1/reminders/alarms}) and
 * reschedules them, without the app being opened: after an "alarm-sync" push (an alarm
 * was set or changed on another device) and after a reboot.
 */
public class AlarmSyncWorker extends Worker {
    public AlarmSyncWorker(@NonNull Context context, @NonNull WorkerParameters params) {
        super(context, params);
    }

    public static void enqueue(Context context) {
        OneTimeWorkRequest request = new OneTimeWorkRequest.Builder(AlarmSyncWorker.class)
                .setConstraints(new Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
                .build();
        WorkManager.getInstance(context).enqueueUniqueWork("seos-alarm-sync", ExistingWorkPolicy.REPLACE, request);
    }

    @NonNull
    @Override
    public Result doWork() {
        SharedPreferences prefs = getApplicationContext().getSharedPreferences("CapacitorStorage", Context.MODE_PRIVATE);
        String server = prefs.getString("seos.server", null);
        String token = prefs.getString("seos.token", null);
        if (server == null || server.isEmpty() || token == null || token.isEmpty()) return Result.success();
        try {
            HttpURLConnection connection = (HttpURLConnection) new URL(server.replaceAll("/+$", "") + "/api/v1/reminders/alarms").openConnection();
            connection.setConnectTimeout(15_000);
            connection.setReadTimeout(20_000);
            connection.setRequestProperty("Accept", "application/json");
            connection.setRequestProperty("Authorization", "Bearer " + token);
            try {
                int status = connection.getResponseCode();
                if (status == 401 || status == 403) return Result.success();  // signed out
                if (status != 200) return Result.retry();
                JSONArray alarms = new JSONObject(read(connection.getInputStream())).getJSONArray("alarms");
                apply(getApplicationContext(), alarms);
                return Result.success();
            } finally {
                connection.disconnect();
            }
        } catch (IOException | JSONException network) {
            return getRunAttemptCount() < 8 ? Result.retry() : Result.failure();
        }
    }

    /** Replaces this phone's alarm list with the server's (progress of unchanged ones is kept). */
    static int apply(Context context, JSONArray alarms) throws JSONException {
        List<AlarmState> incoming = new ArrayList<>();
        for (int i = 0; i < alarms.length(); i++) {
            JSONObject item = alarms.getJSONObject(i);
            String status = item.optString("status", "SCHEDULED");
            if (!"SCHEDULED".equals(status) && !"FIRED".equals(status)) continue;
            AlarmState state = AlarmState.fromServer(item);
            // Answered on another device («Я встал» there): nothing to ring here.
            if (!item.isNull("acknowledged_at") && !item.optString("acknowledged_at", "").isEmpty()) state.phase = AlarmState.DONE;
            incoming.add(state);
        }
        long now = System.currentTimeMillis();
        List<AlarmState> merged = AlarmState.merge(AlarmStore.all(context), incoming, now);
        List<AlarmState> due = new ArrayList<>();
        for (AlarmState state : merged) {
            if (AlarmState.SCHEDULED.equals(state.phase) && state.at <= now) due.add(state);
        }
        AlarmStore.save(context, merged);
        for (AlarmState state : due) AlarmReceiver.ring(context, state, now);
        AlarmScheduler.rescheduleAll(context);
        return merged.size();
    }

    private static String read(InputStream stream) throws IOException {
        try (InputStream in = stream; ByteArrayOutputStream out = new ByteArrayOutputStream()) {
            byte[] buffer = new byte[8192];
            for (int n; (n = in.read(buffer)) > 0; ) out.write(buffer, 0, n);
            return out.toString("UTF-8");
        }
    }
}
