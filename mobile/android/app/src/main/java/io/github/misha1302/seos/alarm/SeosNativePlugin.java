package io.github.misha1302.seos.alarm;

import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Intent;
import android.net.Uri;
import android.os.Build;
import android.os.PowerManager;
import android.provider.Settings;
import androidx.core.app.NotificationCompat;
import androidx.core.app.NotificationManagerCompat;
import androidx.work.WorkManager;
import io.github.misha1302.seos.MainActivity;
import com.getcapacitor.JSArray;
import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;
import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

/**
 * The web client's bridge to what only the phone knows and does: whether it may show
 * notifications and ring exact / full-screen alarms, the system screens to fix that,
 * and the local alarm schedule.
 */
@CapacitorPlugin(name = "SeosNative")
public class SeosNativePlugin extends Plugin {
    @PluginMethod
    public void status(PluginCall call) {
        JSObject out = new JSObject();
        out.put("notifications", NotificationManagerCompat.from(getContext()).areNotificationsEnabled());
        out.put("exact_alarms", AlarmScheduler.exactAllowed(getContext()));
        boolean fullScreen = true;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            NotificationManager manager = getContext().getSystemService(NotificationManager.class);
            fullScreen = manager != null && manager.canUseFullScreenIntent();
        }
        out.put("full_screen", fullScreen);
        PowerManager power = getContext().getSystemService(PowerManager.class);
        out.put("battery_optimized", power != null && !power.isIgnoringBatteryOptimizations(getContext().getPackageName()));
        out.put("sdk", Build.VERSION.SDK_INT);
        int scheduled = 0;
        for (AlarmState state : AlarmStore.all(getContext())) if (state.active() && !state.local) scheduled++;
        out.put("alarms", scheduled);
        call.resolve(out);
    }

    @PluginMethod
    public void openSettings(PluginCall call) {
        String target = call.getString("target", "notifications");
        String pkg = getContext().getPackageName();
        Intent intent;
        if ("exact_alarms".equals(target) && Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            intent = new Intent(Settings.ACTION_REQUEST_SCHEDULE_EXACT_ALARM, Uri.parse("package:" + pkg));
        } else if ("full_screen".equals(target) && Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            intent = new Intent(Settings.ACTION_MANAGE_APP_USE_FULL_SCREEN_INTENT, Uri.parse("package:" + pkg));
        } else if ("battery".equals(target)) {
            intent = new Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS);
        } else if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            intent = new Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS).putExtra(Settings.EXTRA_APP_PACKAGE, pkg);
        } else {
            intent = new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:" + pkg));
        }
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        try {
            getContext().startActivity(intent);
        } catch (RuntimeException missing) {
            getContext().startActivity(new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:" + pkg))
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
        }
        call.resolve();
    }

    /** Replaces the phone's schedule with the account's upcoming alarms (offline-created ones included). */
    @PluginMethod
    public void syncAlarms(PluginCall call) {
        try {
            JSArray alarms = call.getArray("alarms", new JSArray());
            JSONObject labels = call.getObject("labels", null);
            if (labels != null) AlarmStore.setLabels(getContext(), labels);
            // The page hands over the signed-in account's list.
            String owner = AlarmSyncWorker.sessionOwner(getContext());
            int count = AlarmSyncWorker.apply(getContext(), new JSONArray(alarms.toString()), owner);
            JSObject out = new JSObject();
            out.put("scheduled", count);
            out.put("exact", AlarmScheduler.exactAllowed(getContext()));
            call.resolve(out);
        } catch (JSONException malformed) {
            call.reject("malformed alarm list", "INVALID");
        }
    }

    /** End authenticated alarm ownership before credentials/cache are changed. */
    @PluginMethod
    public void clearAlarms(PluginCall call) {
        int removed = AlarmStore.clearAccountAlarms(getContext());
        WorkManager work = WorkManager.getInstance(getContext());
        work.cancelUniqueWork("seos-alarm-sync");
        work.cancelAllWorkByTag("seos-reminder-action");
        JSObject out = new JSObject();
        out.put("removed", removed);
        call.resolve(out);
    }

    private static final String EXECUTION_CHANNEL = "execution";
    private static final int EXECUTION_NOTIFICATION_ID = 4301;

    private PendingIntent executionIntent(String action, String sessionId, int requestCode) {
        Uri uri = Uri.parse("seos://open/today?execution_action=" + Uri.encode(action)
                + "&session_id=" + Uri.encode(sessionId));
        Intent intent = new Intent(getContext(), MainActivity.class)
                .setAction("io.github.misha1302.seos.EXECUTION_" + action)
                .setData(uri)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        return PendingIntent.getActivity(
                getContext(), requestCode, intent,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
        );
    }

    /**
     * One ongoing phone surface for the account's current execution session. The
     * chronometer is entirely OS-side: no second-by-second bridge/server writes.
     * Action intents reopen the explicit app flow; Finish never guesses Task outcome.
     */
    @PluginMethod
    public void showExecution(PluginCall call) {
        String sessionId = call.getString("id");
        String title = call.getString("title", "Execution OS");
        String state = call.getString("state", "ACTIVE");
        Long startedAt = call.getLong("started_at_ms");
        Long actualSeconds = call.getLong("actual_work_seconds", 0L);
        if (sessionId == null || sessionId.isEmpty()) {
            call.reject("execution id is required", "INVALID");
            return;
        }
        NotificationManager manager = getContext().getSystemService(NotificationManager.class);
        if (manager == null) {
            call.reject("notification manager unavailable", "UNAVAILABLE");
            return;
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            NotificationChannel channel = new NotificationChannel(
                    EXECUTION_CHANNEL, "Current work", NotificationManager.IMPORTANCE_LOW
            );
            channel.setDescription("Ongoing Execution OS work session");
            channel.setShowBadge(false);
            manager.createNotificationChannel(channel);
        }
        boolean paused = "PAUSED".equals(state);
        long elapsed = Math.max(0L, actualSeconds == null ? 0L : actualSeconds);
        long base = System.currentTimeMillis() - elapsed * 1000L;
        if (!paused && startedAt != null && startedAt > 0) {
            // actual_work_seconds excludes prior pauses; the current active segment
            // begins at started_at_ms supplied by the web execution projection.
            base = System.currentTimeMillis() - elapsed * 1000L;
        }
        NotificationCompat.Builder builder = new NotificationCompat.Builder(getContext(), EXECUTION_CHANNEL)
                .setSmallIcon(android.R.drawable.ic_media_play)
                .setContentTitle(title)
                .setContentText(paused ? "Paused" : "Execution OS")
                .setOnlyAlertOnce(true)
                .setOngoing(true)
                .setSilent(true)
                .setContentIntent(executionIntent("open", sessionId, 43010))
                .addAction(
                        paused ? android.R.drawable.ic_media_play : android.R.drawable.ic_media_pause,
                        paused ? "Resume" : "Pause",
                        executionIntent(paused ? "resume" : "pause", sessionId, paused ? 43012 : 43011)
                )
                .addAction(
                        android.R.drawable.ic_menu_close_clear_cancel,
                        "Finish",
                        executionIntent("finish", sessionId, 43013)
                );
        if (!paused) {
            builder.setWhen(base).setUsesChronometer(true).setShowWhen(true);
        } else {
            long minutes = elapsed / 60L;
            builder.setContentText("Paused · " + minutes + " min");
        }
        manager.notify("seos-execution", EXECUTION_NOTIFICATION_ID, builder.build());
        call.resolve();
    }

    @PluginMethod
    public void clearExecution(PluginCall call) {
        NotificationManagerCompat.from(getContext()).cancel("seos-execution", EXECUTION_NOTIFICATION_ID);
        call.resolve();
    }

    /** A local alarm in five seconds (not a reminder on the server) to hear and see it. */
    @PluginMethod
    public void testAlarm(PluginCall call) {
        long at = System.currentTimeMillis() + 5_000L;
        AlarmState test = new AlarmState("test-" + at, at, AlarmStore.label(getContext(), "test_title"), false, false, true);
        AlarmStore.put(getContext(), test);
        AlarmScheduler.rescheduleAll(getContext());
        call.resolve();
    }
}
