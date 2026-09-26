package io.github.misha1302.seos.alarm;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

/**
 * One alarm on this phone and where it is in its life. Pure logic (no Android types),
 * unit-tested on the JVM; {@link AlarmStore} persists it and {@link AlarmScheduler}
 * turns {@link #next}/{@link #nextKind} into an AlarmManager alarm clock.
 *
 * <pre>
 * SCHEDULED ─fire─▶ RINGING ─«Я встал»─▶ AWAKE_WAIT ─25 min─▶ CHECKING ─«Не сплю»─▶ DONE
 *     ▲               │  │                                      │
 *     │   ring 5 min, │  └─«Отложить»─▶ SNOOZED ─▶ RINGING       └─10 min, no answer─▶ RINGING
 *     │   no answer   ▼
 *     └──────────── SNOOZED (5 min, at most 3 rounds, then MISSED)
 * </pre>
 * A plain alarm (not a wake alarm) is DONE after «Выключить»/«Готово».
 */
public final class AlarmState {
    public static final long MINUTE = 60_000L;
    public static final long RING_LIMIT = 5 * MINUTE;
    public static final long AUTO_SNOOZE = 5 * MINUTE;
    public static final long AWAKE_CHECK_AFTER = 25 * MINUTE;
    public static final long REWAKE_AFTER = 10 * MINUTE;
    public static final long SNOOZE = 10 * MINUTE;
    /** A fire moment older than this when first seen (sync after being off) is not rung. */
    public static final long STALE_AFTER = 15 * MINUTE;
    public static final int MAX_ROUNDS = 3;

    public static final String SCHEDULED = "SCHEDULED";
    public static final String RINGING = "RINGING";
    public static final String SNOOZED = "SNOOZED";
    public static final String AWAKE_WAIT = "AWAKE_WAIT";
    public static final String CHECKING = "CHECKING";
    public static final String DONE = "DONE";
    public static final String MISSED = "MISSED";

    /** What the next AlarmManager event does. */
    public static final String FIRE = "FIRE";
    public static final String AWAKE_CHECK = "AWAKE_CHECK";
    public static final String REWAKE = "REWAKE";

    public final String id;
    public final long at;
    public final String title;
    public final boolean wakeCheck;
    public final boolean raiseVolume;
    /** Local-only (the test alarm from Settings): nothing is reported to the server. */
    public final boolean local;
    /** «Я встал» was answered for this episode, on this or another phone (server-owned). */
    public final boolean acknowledged;
    public String phase = SCHEDULED;
    public int round = 0;
    public long next;
    public String nextKind = FIRE;

    public AlarmState(String id, long at, String title, boolean wakeCheck, boolean raiseVolume, boolean local) {
        this(id, at, title, wakeCheck, raiseVolume, local, false);
    }

    public AlarmState(String id, long at, String title, boolean wakeCheck, boolean raiseVolume, boolean local,
                      boolean acknowledged) {
        this.id = id;
        this.at = at;
        this.title = title;
        this.wakeCheck = wakeCheck;
        this.raiseVolume = raiseVolume;
        this.local = local;
        this.acknowledged = acknowledged;
        this.next = at;
    }

    /** Same reminder and same moment: a server resync must not reset its progress. */
    public String key() {
        return id + "|" + at;
    }

    public boolean pending() {
        return !DONE.equals(phase) && !MISSED.equals(phase) && !RINGING.equals(phase);
    }

    public boolean active() {
        return !DONE.equals(phase) && !MISSED.equals(phase);
    }

    // ---- transitions ------------------------------------------------------------------

    public void fire(long now) {
        phase = RINGING;
        round += 1;
        next = now + RING_LIMIT;
        nextKind = null;
    }

    /** Rang RING_LIMIT without an answer: rest a little and ring again, a few times. */
    public void ringTimeout(long now) {
        if (round >= MAX_ROUNDS) {
            phase = MISSED;
            nextKind = null;
            return;
        }
        phase = SNOOZED;
        next = now + AUTO_SNOOZE;
        nextKind = FIRE;
    }

    /** «Я встал» / «Выключить». */
    public void up(long now) {
        if (wakeCheck) {
            phase = AWAKE_WAIT;
            next = now + AWAKE_CHECK_AFTER;
            nextKind = AWAKE_CHECK;
        } else {
            phase = DONE;
            nextKind = null;
        }
    }

    public void awakeCheck(long now) {
        phase = CHECKING;
        next = now + REWAKE_AFTER;
        nextKind = REWAKE;
    }

    /** «Не сплю». */
    public void awake() {
        phase = DONE;
        nextKind = null;
    }

    public void snooze(long now) {
        phase = SNOOZED;
        next = now + SNOOZE;
        nextKind = FIRE;
    }

    /** No answer to the awake check: wake the user again (a fresh set of rounds). */
    public void rewake(long now) {
        round = 0;
        fire(now);
    }

    // ---- sync with the server's list -----------------------------------------------------

    /**
     * Merges the server's upcoming alarms into what this phone has. The server owns the
     * configuration (title, wake check, volume) and whether the episode was answered;
     * the phone owns its execution progress (phase, round, next event).
     * <ul>
     * <li>Same reminder and moment: fresh configuration plus this phone's progress.</li>
     * <li>Answered («Я встал») on another phone: stop here. The phone that was answered
     *     keeps its own awake check (it is in AWAKE_WAIT/CHECKING).</li>
     * <li>A new moment is a new episode; removed episodes go (the local test alarm stays).</li>
     * </ul>
     * A moment already past by more than {@link #STALE_AFTER} when first seen is not rung.
     */
    public static List<AlarmState> merge(List<AlarmState> current, List<AlarmState> incoming, long now) {
        Map<String, AlarmState> byKey = new LinkedHashMap<>();
        for (AlarmState state : current) byKey.put(state.key(), state);
        List<AlarmState> result = new ArrayList<>();
        for (AlarmState fresh : incoming) {
            AlarmState known = byKey.remove(fresh.key());
            boolean answeredHere = known != null && (AWAKE_WAIT.equals(known.phase)
                    || CHECKING.equals(known.phase) || DONE.equals(known.phase));
            if (fresh.acknowledged && !answeredHere) {
                fresh.phase = DONE;
                fresh.nextKind = null;
            } else if (known == null) {
                if (fresh.at < now - STALE_AFTER) {
                    fresh.phase = MISSED;
                    fresh.nextKind = null;
                }
            } else if (!fresh.wakeCheck && (AWAKE_WAIT.equals(known.phase) || CHECKING.equals(known.phase))) {
                // Made a plain alarm after it was answered: nothing is left to check.
                fresh.phase = DONE;
                fresh.nextKind = null;
            } else {
                fresh.phase = known.phase;
                fresh.round = known.round;
                fresh.next = known.next;
                fresh.nextKind = known.nextKind;
            }
            result.add(fresh);
        }
        for (AlarmState left : byKey.values()) {
            if (left.local) result.add(left);
        }
        return result;
    }

    /** The local test alarm is device-owned; all other alarms belong to the session. */
    public static List<AlarmState> withoutAccountAlarms(List<AlarmState> current) {
        List<AlarmState> result = new ArrayList<>();
        for (AlarmState state : current) if (state.local) result.add(state);
        return result;
    }

    // ---- JSON ---------------------------------------------------------------------------

    public static AlarmState fromServer(JSONObject json) throws JSONException {
        String at = json.optString("at", "");
        String acknowledged = json.isNull("acknowledged_at") ? "" : json.optString("acknowledged_at", "");
        return new AlarmState(json.getString("id"), Iso.parse(at.isEmpty() ? json.getString("remind_at") : at),
                json.optString("title", ""), json.optBoolean("wake_check", false), json.optBoolean("raise_volume", false),
                false, !acknowledged.isEmpty());
    }

    public JSONObject toJson() throws JSONException {
        return new JSONObject().put("id", id).put("at", at).put("title", title).put("wake", wakeCheck)
                .put("loud", raiseVolume).put("local", local).put("ack", acknowledged).put("phase", phase).put("round", round)
                .put("next", next).put("nextKind", nextKind == null ? JSONObject.NULL : nextKind);
    }

    public static AlarmState fromJson(JSONObject json) throws JSONException {
        AlarmState state = new AlarmState(json.getString("id"), json.getLong("at"), json.optString("title", ""),
                json.optBoolean("wake"), json.optBoolean("loud"), json.optBoolean("local"), json.optBoolean("ack"));
        state.phase = json.optString("phase", SCHEDULED);
        state.round = json.optInt("round", 0);
        state.next = json.optLong("next", state.at);
        state.nextKind = json.isNull("nextKind") ? null : json.optString("nextKind", FIRE);
        return state;
    }

    public static List<AlarmState> listFromJson(String text) {
        List<AlarmState> out = new ArrayList<>();
        try {
            JSONArray array = new JSONArray(text == null || text.isEmpty() ? "[]" : text);
            for (int i = 0; i < array.length(); i++) out.add(fromJson(array.getJSONObject(i)));
        } catch (JSONException broken) {
            // A damaged store is rebuilt by the next sync from the server.
        }
        return out;
    }

    public static String listToJson(List<AlarmState> states) {
        JSONArray array = new JSONArray();
        for (AlarmState state : states) {
            try {
                array.put(state.toJson());
            } catch (JSONException ignored) {
                // cannot happen for plain values
            }
        }
        return array.toString();
    }
}
