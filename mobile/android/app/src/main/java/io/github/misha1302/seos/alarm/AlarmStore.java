package io.github.misha1302.seos.alarm;

import android.content.Context;
import android.content.SharedPreferences;
import java.util.List;
import java.util.Locale;
import org.json.JSONException;
import org.json.JSONObject;

/**
 * Alarms this phone keeps, in SharedPreferences so they survive process death and
 * reboots, plus the alarm volume to restore after an alarm raised it.
 */
public final class AlarmStore {
    private static final String PREFS = "seos.alarms";
    private static final String ALARMS = "alarms";
    private static final String LABELS = "labels";
    private static final String VOLUME = "volume_restore";
    private static final String OWNER = "session_owner";

    private AlarmStore() {}

    private static SharedPreferences prefs(Context context) {
        return context.getApplicationContext().getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    public static synchronized List<AlarmState> all(Context context) {
        return AlarmState.listFromJson(prefs(context).getString(ALARMS, "[]"));
    }

    public static synchronized void save(Context context, List<AlarmState> states) {
        // Finished alarms are kept a day (dedupe of a late push), then forgotten.
        long cutoff = System.currentTimeMillis() - 24 * 3600_000L;
        states.removeIf(s -> !s.active() && s.at < cutoff);
        prefs(context).edit().putString(ALARMS, AlarmState.listToJson(states)).apply();
    }

    public static synchronized String owner(Context context) {
        return prefs(context).getString(OWNER, null);
    }

    public static synchronized void setOwner(Context context, String owner) {
        SharedPreferences.Editor edit = prefs(context).edit();
        if (owner == null || owner.isEmpty()) edit.remove(OWNER); else edit.putString(OWNER, owner);
        edit.apply();
    }

    /** Remove everything owned by the authenticated account, preserving local tests. */
    public static synchronized int clearAccountAlarms(Context context) {
        List<AlarmState> current = all(context);
        int removed = 0;
        for (AlarmState state : current) {
            if (state.local) continue;
            removed++;
            AlarmScheduler.cancel(context, state);
            AlarmNotifications.cancelAll(context, state);
            if (AlarmState.RINGING.equals(state.phase)) AlarmService.stop(context, state.id);
        }
        save(context, AlarmState.withoutAccountAlarms(current));
        setOwner(context, null);
        AlarmService.restoreVolume(context);
        return removed;
    }

    public static synchronized AlarmState get(Context context, String id) {
        for (AlarmState state : all(context)) if (state.id.equals(id)) return state;
        return null;
    }

    public static synchronized void put(Context context, AlarmState updated) {
        List<AlarmState> states = all(context);
        boolean replaced = false;
        for (int i = 0; i < states.size(); i++) {
            if (states.get(i).id.equals(updated.id)) {
                states.set(i, updated);
                replaced = true;
            }
        }
        if (!replaced) states.add(updated);
        save(context, states);
    }

    // ---- texts (the web client and pushes provide them in the account's language) --------

    public static synchronized void setLabels(Context context, JSONObject labels) {
        if (labels != null && labels.length() > 0) prefs(context).edit().putString(LABELS, labels.toString()).apply();
    }

    public static synchronized String label(Context context, String key) {
        try {
            JSONObject labels = new JSONObject(prefs(context).getString(LABELS, "{}"));
            if (labels.has(key)) return labels.getString(key);
        } catch (JSONException ignored) {
            // fall back to the built-in texts
        }
        boolean ru = Locale.getDefault().getLanguage().equals("ru");
        switch (key) {
            case "alarm_up": return ru ? "Я встал" : "I'm up";
            case "alarm_done": return ru ? "Выключить" : "Turn off";
            case "alarm_snooze": return ru ? "Отложить на 10 мин" : "Snooze 10 min";
            case "awake_title": return ru ? "Вы не уснули?" : "Still awake?";
            case "awake_body": return ru ? "Нажмите «Не сплю», иначе через 10 минут будильник зазвонит снова"
                    : "Tap “I'm awake”, or the alarm rings again in 10 minutes";
            case "awake_ok": return ru ? "Не сплю" : "I'm awake";
            case "awake_wait": return ru ? "Проверю через 25 минут, что вы не уснули" : "I'll check in 25 minutes that you're awake";
            case "alarm_missed": return ru ? "Будильник пропущен: «{title}»" : "Missed alarm: “{title}”";
            case "test_title": return ru ? "Проверка будильника" : "Alarm test";
            default: return "";
        }
    }

    // ---- volume -----------------------------------------------------------------------

    // commit(): the original volume must be on disk before the alarm raises it, so a
    // crash or reboot mid-alarm can still restore it.
    @android.annotation.SuppressLint("ApplySharedPref")
    public static synchronized void rememberVolume(Context context, int volume) {
        if (!prefs(context).contains(VOLUME)) prefs(context).edit().putInt(VOLUME, volume).commit();
    }

    /** The volume to restore, or -1; reading it clears it. */
    @android.annotation.SuppressLint("ApplySharedPref")
    public static synchronized int takeVolume(Context context) {
        int volume = prefs(context).getInt(VOLUME, -1);
        prefs(context).edit().remove(VOLUME).commit();
        return volume;
    }
}
