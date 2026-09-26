package io.github.misha1302.seos.alarm;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import io.github.misha1302.seos.reminders.ReminderActionWorker;

/**
 * Alarm events from AlarmManager (fire, awake check, re-wake) and the user's answers
 * from the alarm screen or its notification («Я встал», «Не сплю», «Отложить»).
 * Every answer changes the local state at once and is reported to the server through
 * the same durable WorkManager queue as notification buttons (works offline).
 */
public class AlarmReceiver extends BroadcastReceiver {
    static final String EXTRA_ID = "seos.alarm.id";
    static final String EXTRA_KIND = "seos.alarm.kind";
    static final String ACTION_UP = "io.github.misha1302.seos.ALARM_UP";
    static final String ACTION_AWAKE = "io.github.misha1302.seos.ALARM_AWAKE";
    static final String ACTION_SNOOZE = "io.github.misha1302.seos.ALARM_SNOOZE";
    static final String ACTION_TIMEOUT = "io.github.misha1302.seos.ALARM_TIMEOUT";

    @Override
    public void onReceive(Context context, Intent intent) {
        String id = intent.getStringExtra(EXTRA_ID);
        if (id == null || intent.getAction() == null) return;
        handle(context, intent.getAction(), id, intent.getStringExtra(EXTRA_KIND));
    }

    static void handle(Context context, String action, String id, String kind) {
        AlarmState state = AlarmStore.get(context, id);
        if (state == null || !state.active()) {
            AlarmService.stop(context, id);
            return;
        }
        long now = System.currentTimeMillis();
        switch (action) {
            case AlarmScheduler.ACTION_EVENT:
                if (AlarmState.FIRE.equals(kind) && (AlarmState.SCHEDULED.equals(state.phase) || AlarmState.SNOOZED.equals(state.phase))) {
                    ring(context, state, now);
                } else if (AlarmState.AWAKE_CHECK.equals(kind) && AlarmState.AWAKE_WAIT.equals(state.phase)) {
                    state.awakeCheck(now);
                    AlarmStore.put(context, state);
                    AlarmNotifications.showAwakeCheck(context, state);
                } else if (AlarmState.REWAKE.equals(kind) && AlarmState.CHECKING.equals(state.phase)) {
                    AlarmNotifications.cancelAwakeCheck(context, state);
                    state.rewake(now);
                    AlarmStore.put(context, state);
                    AlarmService.start(context, state.id);
                }
                break;
            case ACTION_UP:
                if (!AlarmState.RINGING.equals(state.phase) && !AlarmState.SNOOZED.equals(state.phase)) break;
                state.up(now);
                AlarmStore.put(context, state);
                AlarmService.stop(context, id);
                report(context, state, AlarmOps.up(state), "UP");
                if (state.wakeCheck) AlarmNotifications.showAwakeWait(context, state);
                break;
            case ACTION_AWAKE:
                if (!AlarmState.CHECKING.equals(state.phase) && !AlarmState.AWAKE_WAIT.equals(state.phase)) break;
                state.awake();
                AlarmStore.put(context, state);
                AlarmNotifications.cancelAwakeCheck(context, state);
                report(context, state, AlarmOps.awake(state), "AWAKE");
                break;
            case ACTION_SNOOZE:
                state.snooze(now);
                AlarmStore.put(context, state);
                AlarmService.stop(context, id);
                report(context, state, AlarmOps.snooze(state, state.next), "SNOOZE-" + state.next);
                break;
            case ACTION_TIMEOUT:
                if (!AlarmState.RINGING.equals(state.phase)) break;
                state.ringTimeout(now);
                AlarmStore.put(context, state);
                AlarmService.stop(context, id);
                if (AlarmState.MISSED.equals(state.phase)) AlarmNotifications.showMissed(context, state);
                break;
            default:
                return;
        }
        AlarmScheduler.rescheduleAll(context);
    }

    /** Rings now (from AlarmManager, a push, or a sync that finds it due). */
    static void ring(Context context, AlarmState state, long now) {
        state.fire(now);
        AlarmStore.put(context, state);
        AlarmService.start(context, state.id);
    }

    private static void report(Context context, AlarmState state, String operations, String suffix) {
        if (state.local) return;  // the test alarm is not a reminder on the server
        ReminderActionWorker.enqueue(context, "seos-alarm-" + state.id + "-" + state.at + "-" + suffix, operations,
                "seos-alarm:" + state.id, "", "/today");
    }
}
