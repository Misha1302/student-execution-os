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
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
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
        String owner = sessionOwner(getApplicationContext());
        if (owner == null) return Result.success();  // signed out
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
                // Signed out or switched while the request was in flight: apply() drops it.
                apply(getApplicationContext(), alarms, owner);
                return Result.success();
            } finally {
                connection.disconnect();
            }
        } catch (IOException | JSONException network) {
            return getRunAttemptCount() < 8 ? Result.retry() : Result.failure();
        }
    }

    /**
     * Replaces this phone's alarm list with the server's (progress of unchanged ones is
     * kept), if the list still belongs to the signed-in account: {@code owner} is who it
     * was fetched for. The check and the write happen under the store's lock, the same
     * lock {@link AlarmStore#clearAccountAlarms} takes, so a response that races a logout
     * or an account switch can never bring the previous account's alarms back.
     */
    static int apply(Context context, JSONArray alarms, String owner) throws JSONException {
        synchronized (AlarmStore.class) {
            String current = sessionOwner(context);
            if (current == null || !current.equals(owner)) return 0;
            String previous = AlarmStore.owner(context);
            if (previous != null && !previous.equals(owner)) AlarmStore.clearAccountAlarms(context);
            AlarmStore.setOwner(context, owner);
            return merge(context, alarms);
        }
    }

    private static int merge(Context context, JSONArray alarms) throws JSONException {
        List<AlarmState> incoming = new ArrayList<>();
        for (int i = 0; i < alarms.length(); i++) {
            JSONObject item = alarms.getJSONObject(i);
            String status = item.optString("status", "SCHEDULED");
            if (!"SCHEDULED".equals(status) && !"FIRED".equals(status)) continue;
            incoming.add(AlarmState.fromServer(item));
        }
        long now = System.currentTimeMillis();
        List<AlarmState> current = AlarmStore.all(context);
        List<AlarmState> merged = AlarmState.merge(current, incoming, now);
        List<AlarmState> refreshRinging = new ArrayList<>();
        for (AlarmState old : current) {
            AlarmState kept = null;
            for (AlarmState state : merged) {
                if (state.key().equals(old.key()) && state.active()) { kept = state; break; }
            }
            if (kept == null) {
                // Removed, moved, or answered elsewhere: nothing of it may ring here.
                AlarmScheduler.cancel(context, old);
                AlarmNotifications.cancelAll(context, old);
                if (AlarmState.RINGING.equals(old.phase)) AlarmService.stop(context, old.id);
            } else if (AlarmState.RINGING.equals(old.phase) && (!old.title.equals(kept.title)
                    || old.wakeCheck != kept.wakeCheck || old.raiseVolume != kept.raiseVolume)) {
                refreshRinging.add(kept);  // ringing now: apply the new title/volume at once
            }
        }
        List<AlarmState> due = new ArrayList<>();
        for (AlarmState state : merged) {
            if (AlarmState.SCHEDULED.equals(state.phase) && state.at <= now) due.add(state);
        }
        AlarmStore.save(context, merged);
        for (AlarmState state : refreshRinging) AlarmService.start(context, state.id);
        for (AlarmState state : due) AlarmReceiver.ring(context, state, now);
        AlarmScheduler.rescheduleAll(context);
        return merged.size();
    }

    /**
     * Who this phone's alarms belong to: the signed-in account on its server, or null
     * when signed out. A re-issued token of the same account keeps the ownership (and
     * the progress of a ringing alarm); another account or server does not.
     */
    static String sessionOwner(Context context) {
        SharedPreferences prefs = context.getSharedPreferences("CapacitorStorage", Context.MODE_PRIVATE);
        String server = prefs.getString("seos.server", "");
        String token = prefs.getString("seos.token", "");
        if (server == null || server.isEmpty() || token == null || token.isEmpty()) return null;
        String account = sessionAccount(prefs);
        String who = account == null ? "token:" + sha256(token) : "account:" + account;
        return server.replaceAll("/+$", "") + "\n" + who;
    }

    /**
     * Whether a push for {@code accountId} belongs to the signed-in session. A signed-out
     * phone accepts none; a push without an account (older server) is accepted.
     */
    public static boolean belongsToSession(Context context, String accountId) {
        if (sessionOwner(context) == null) return false;
        if (accountId == null || accountId.isEmpty()) return true;
        String account = sessionAccount(context.getSharedPreferences("CapacitorStorage", Context.MODE_PRIVATE));
        return account == null || account.equals(accountId);
    }

    private static String sessionAccount(SharedPreferences prefs) {
        try {
            String account = new JSONObject(prefs.getString("seos.user", "{}")).optString("account_id", "");
            return account.isEmpty() ? null : account;
        } catch (JSONException unreadable) {
            return null;
        }
    }

    private static String sha256(String value) {
        try {
            byte[] digest = MessageDigest.getInstance("SHA-256").digest(value.getBytes(StandardCharsets.UTF_8));
            StringBuilder out = new StringBuilder();
            for (byte b : digest) out.append(String.format("%02x", b & 0xff));
            return out.toString();
        } catch (NoSuchAlgorithmException impossible) {
            throw new IllegalStateException(impossible);
        }
    }

    private static String read(InputStream stream) throws IOException {
        try (InputStream in = stream; ByteArrayOutputStream out = new ByteArrayOutputStream()) {
            byte[] buffer = new byte[8192];
            for (int n; (n = in.read(buffer)) > 0; ) out.write(buffer, 0, n);
            return out.toString("UTF-8");
        }
    }
}
