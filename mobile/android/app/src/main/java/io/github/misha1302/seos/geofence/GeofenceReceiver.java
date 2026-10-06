package io.github.misha1302.seos.geofence;

import android.app.PendingIntent;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.location.LocationManager;
import android.os.Build;
import androidx.core.app.NotificationCompat;
import androidx.core.app.NotificationManagerCompat;
import io.github.misha1302.seos.R;
import io.github.misha1302.seos.alarm.AlarmSyncWorker;
import io.github.misha1302.seos.reminders.ReminderActionWorker;
import io.github.misha1302.seos.reminders.ReminderNotifications;
import java.util.Locale;

/**
 * A platform proximity crossing. The phone tells the user at once (it may be offline)
 * and queues the firing for the server; the server owns the trigger's state and
 * ignores a second report of the same crossing.
 */
public class GeofenceReceiver extends BroadcastReceiver {
    static final String EXTRA_TRIGGER = "seos.trigger";
    static final String EXTRA_DONE = "seos.trigger_done";

    @Override
    public void onReceive(Context context, Intent intent) {
        String triggerId = intent.getStringExtra(EXTRA_TRIGGER);
        if (triggerId == null) return;
        if (intent.getBooleanExtra(EXTRA_DONE, false)) {
            ReminderActionWorker.enqueue(context, "geo-done-" + triggerId, GeofenceDecisions.doneOperations(triggerId),
                    "seos:geo:" + triggerId, "", "/places");
            NotificationManagerCompat.from(context).cancel("seos:geo:" + triggerId, NOTIFICATION_ID);
            return;
        }
        if (!intent.hasExtra(LocationManager.KEY_PROXIMITY_ENTERING)) return;
        boolean entering = intent.getBooleanExtra(LocationManager.KEY_PROXIMITY_ENTERING, false);
        GeofenceSpec spec = GeofenceStore.find(context, triggerId);
        // Another account signed in meanwhile (or signed out): nothing of the old one fires.
        String owner = AlarmSyncWorker.sessionOwnerOf(context);
        if (spec == null || owner == null || !owner.equals(GeofenceStore.owner(context))) {
            GeofenceRegistrar.forget(context, triggerId);
            return;
        }
        long now = System.currentTimeMillis();
        if (!GeofenceDecisions.shouldFire(spec, entering, GeofenceStore.lastFired(context, triggerId), now)) return;
        GeofenceStore.setLastFired(context, triggerId, now);
        String operations = GeofenceDecisions.fireOperations(triggerId, spec.transition, now);
        ReminderActionWorker.enqueue(context, "geo-fire-" + triggerId + "-" + (now / 600_000L), operations,
                "seos:geo:" + triggerId, "", "/places");
        if (!spec.repeat) GeofenceRegistrar.forget(context, triggerId);  // one-shot: stop watching here
        show(context, spec);
    }

    static final int NOTIFICATION_ID = 7301;

    static void show(Context context, GeofenceSpec spec) {
        ReminderNotifications.ensureChannelPublic(context);
        boolean ru = Locale.getDefault().getLanguage().startsWith("ru");
        String body = "ENTER".equals(spec.transition)
                ? (ru ? "Вы пришли: " : "You arrived: ") + spec.placeName
                : (ru ? "Вы ушли: " : "You left: ") + spec.placeName;
        Intent done = new Intent(context, GeofenceReceiver.class).setAction("io.github.misha1302.seos.GEOFENCE_DONE")
                .putExtra(EXTRA_TRIGGER, spec.id).putExtra(EXTRA_DONE, true);
        PendingIntent doneIntent = PendingIntent.getBroadcast(context, GeofenceDecisions.requestCode(spec.id) + 1, done,
                PendingIntent.FLAG_UPDATE_CURRENT | (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M ? PendingIntent.FLAG_IMMUTABLE : 0));
        NotificationCompat.Builder builder = new NotificationCompat.Builder(context, ReminderNotifications.CHANNEL)
                .setSmallIcon(R.drawable.ic_stat_reminder)
                .setContentTitle("📍 " + spec.title)
                .setContentText(body)
                .setPriority(NotificationCompat.PRIORITY_HIGH)
                .setCategory(NotificationCompat.CATEGORY_REMINDER)
                .setAutoCancel(true)
                .addAction(0, ru ? "Готово" : "Done", doneIntent);
        try {
            NotificationManagerCompat.from(context).notify("seos:geo:" + spec.id, NOTIFICATION_ID, builder.build());
        } catch (SecurityException denied) {
            // Notifications not allowed: the firing still reaches the server and the app.
        }
    }
}
