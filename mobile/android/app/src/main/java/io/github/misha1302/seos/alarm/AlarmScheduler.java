package io.github.misha1302.seos.alarm;

import android.app.AlarmManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import io.github.misha1302.seos.MainActivity;
import java.util.List;

/**
 * Puts each alarm's next event into AlarmManager.
 *
 * <p>{@code setAlarmClock} is exact, fires in Doze and shows the alarm icon; it needs
 * the "Alarms & reminders" permission on Android 12+. Without it the event is set with
 * {@code setAndAllowWhileIdle} (may be minutes late) and Settings says so.
 */
public final class AlarmScheduler {
    static final String ACTION_EVENT = "io.github.misha1302.seos.ALARM_EVENT";

    private AlarmScheduler() {}

    public static boolean exactAllowed(Context context) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.S) return true;
        AlarmManager manager = context.getSystemService(AlarmManager.class);
        return manager != null && manager.canScheduleExactAlarms();
    }

    private static PendingIntent event(Context context, AlarmState state, String kind, int flags) {
        Intent intent = new Intent(context, AlarmReceiver.class).setAction(ACTION_EVENT)
                .putExtra(AlarmReceiver.EXTRA_ID, state.id).putExtra(AlarmReceiver.EXTRA_KIND, kind);
        return PendingIntent.getBroadcast(context, (state.id + "|" + kind).hashCode(), intent, flags | PendingIntent.FLAG_IMMUTABLE);
    }

    /** Cancels every pending event of every known alarm, then schedules what is due next. */
    public static void rescheduleAll(Context context) {
        List<AlarmState> states = AlarmStore.all(context);
        for (AlarmState state : states) {
            cancel(context, state);
            if (state.active() && state.nextKind != null) schedule(context, state);
        }
    }

    public static void cancel(Context context, AlarmState state) {
        AlarmManager manager = context.getSystemService(AlarmManager.class);
        for (String kind : new String[] {AlarmState.FIRE, AlarmState.AWAKE_CHECK, AlarmState.REWAKE}) {
            PendingIntent existing = event(context, state, kind, PendingIntent.FLAG_NO_CREATE);
            if (existing != null && manager != null) {
                manager.cancel(existing);
                existing.cancel();
            }
        }
    }

    public static void schedule(Context context, AlarmState state) {
        AlarmManager manager = context.getSystemService(AlarmManager.class);
        if (manager == null) return;
        PendingIntent operation = event(context, state, state.nextKind, PendingIntent.FLAG_UPDATE_CURRENT);
        long at = Math.max(state.next, System.currentTimeMillis() + 1000);
        if (exactAllowed(context)) {
            Intent open = new Intent(context, MainActivity.class).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            PendingIntent show = PendingIntent.getActivity(context, 7300, open, PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
            manager.setAlarmClock(new AlarmManager.AlarmClockInfo(at, show), operation);
        } else {
            manager.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, at, operation);
        }
    }
}
