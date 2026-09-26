package io.github.misha1302.seos.alarm;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/**
 * AlarmManager forgets everything on reboot, an app update, a clock or time-zone
 * change, or when the exact-alarm permission changes: the alarms are put back from
 * {@link AlarmStore}. A volume left raised by an interrupted alarm is restored.
 */
public class BootReceiver extends BroadcastReceiver {
    private static final java.util.Set<String> ACTIONS = new java.util.HashSet<>(java.util.Arrays.asList(
            Intent.ACTION_BOOT_COMPLETED, Intent.ACTION_MY_PACKAGE_REPLACED, Intent.ACTION_TIME_CHANGED,
            Intent.ACTION_TIMEZONE_CHANGED, "android.app.action.SCHEDULE_EXACT_ALARM_PERMISSION_STATE_CHANGED"));

    @Override
    public void onReceive(Context context, Intent intent) {
        if (intent == null || !ACTIONS.contains(intent.getAction())) return;
        AlarmService.restoreVolume(context);
        long now = System.currentTimeMillis();
        java.util.List<AlarmState> states = AlarmStore.all(context);
        for (AlarmState state : states) {
            // Rang while the phone was off: ring once now if it is recent, else it was missed.
            if (AlarmState.RINGING.equals(state.phase)) state.ringTimeout(now);
        }
        AlarmStore.save(context, states);
        AlarmScheduler.rescheduleAll(context);
        AlarmSyncWorker.enqueue(context);
    }
}
