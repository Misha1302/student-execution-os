package io.github.misha1302.seos.geofence;

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
import io.github.misha1302.seos.alarm.AlarmSyncWorker;
import io.github.misha1302.seos.storage.SessionCredentials;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.List;
import org.json.JSONException;
import org.json.JSONObject;

/**
 * Fetches the armed triggers ({@code GET /api/v1/location-triggers/armed}) and registers
 * them: on app start/resume, after a trigger changed on another device (the "alarm-sync"
 * push), after a reboot or an update.
 */
public class GeofenceSyncWorker extends Worker {
    public GeofenceSyncWorker(@NonNull Context context, @NonNull WorkerParameters params) {
        super(context, params);
    }

    public static void enqueue(Context context) {
        OneTimeWorkRequest request = new OneTimeWorkRequest.Builder(GeofenceSyncWorker.class)
                .setConstraints(new Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
                .build();
        WorkManager.getInstance(context).enqueueUniqueWork("seos-geofence-sync", ExistingWorkPolicy.REPLACE, request);
    }

    @NonNull
    @Override
    public Result doWork() {
        Context context = getApplicationContext();
        SharedPreferences prefs = context.getSharedPreferences("CapacitorStorage", Context.MODE_PRIVATE);
        String server = prefs.getString("seos.server", null);
        String token = SessionCredentials.of(context).token();
        String owner = AlarmSyncWorker.sessionOwnerOf(context);
        if (owner == null || server == null) {
            GeofenceRegistrar.clear(context);
            return Result.success();
        }
        try {
            HttpURLConnection connection = (HttpURLConnection) new URL(server.replaceAll("/+$", "")
                    + "/api/v1/location-triggers/armed").openConnection();
            connection.setConnectTimeout(15_000);
            connection.setReadTimeout(20_000);
            connection.setRequestProperty("Accept", "application/json");
            connection.setRequestProperty("Authorization", "Bearer " + token);
            try {
                int status = connection.getResponseCode();
                if (status == 401 || status == 403) {
                    GeofenceRegistrar.clear(context);
                    return Result.success();
                }
                if (status != 200) return Result.retry();
                List<GeofenceSpec> specs = GeofenceSpec.listFromJson(new JSONObject(read(connection.getInputStream()))
                        .getJSONArray("triggers"));
                apply(context, specs, owner);
                return Result.success();
            } finally {
                connection.disconnect();
            }
        } catch (IOException | JSONException | IllegalArgumentException network) {
            return getRunAttemptCount() < 8 ? Result.retry() : Result.failure();
        }
    }

    /** Applies the list if it still belongs to the signed-in account (an account switch clears first). */
    static int apply(Context context, List<GeofenceSpec> specs, String owner) {
        synchronized (GeofenceStore.class) {
            String current = AlarmSyncWorker.sessionOwnerOf(context);
            if (current == null || !current.equals(owner)) return 0;
            String previous = GeofenceStore.owner(context);
            if (previous != null && !previous.equals(owner)) GeofenceRegistrar.clear(context);
            GeofenceStore.setOwner(context, owner);
            return GeofenceRegistrar.apply(context, specs);
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
