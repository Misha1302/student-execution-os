package io.github.misha1302.seos.alarm;

import android.app.NotificationManager;
import android.content.Intent;
import android.net.Uri;
import android.os.Build;
import android.os.PowerManager;
import android.provider.Settings;
import androidx.core.app.NotificationManagerCompat;
import androidx.work.WorkManager;
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
