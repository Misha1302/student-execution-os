package io.github.misha1302.seos.alarm;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.List;
import org.json.JSONArray;
import org.json.JSONObject;
import org.junit.Test;

public class AlarmStateTest {
    private static final long NOW = 1_790_299_980_000L;  // minute-aligned
    private static final long M = AlarmState.MINUTE;

    private static AlarmState wake() {
        return new AlarmState("reminder-wake", NOW, "Подъём", true, true, false);
    }

    @Test
    public void wakeAlarmAsksAgainAfterIAmUpAndRingsAgainWithoutAnAnswer() {
        AlarmState state = wake();
        state.fire(NOW);
        assertEquals(AlarmState.RINGING, state.phase);
        state.up(NOW + M);
        assertEquals(AlarmState.AWAKE_WAIT, state.phase);
        assertEquals(AlarmState.AWAKE_CHECK, state.nextKind);
        assertEquals(NOW + M + 25 * M, state.next);
        state.awakeCheck(state.next);
        assertEquals(AlarmState.CHECKING, state.phase);
        assertEquals(AlarmState.REWAKE, state.nextKind);
        long rewakeAt = state.next;
        assertEquals(NOW + 36 * M, rewakeAt);
        state.rewake(rewakeAt);
        assertEquals(AlarmState.RINGING, state.phase);
        assertEquals(1, state.round);
        state.up(rewakeAt + M);
        state.awakeCheck(state.next);
        state.awake();
        assertEquals(AlarmState.DONE, state.phase);
        assertNull(state.nextKind);
    }

    @Test
    public void unansweredRingingRepeatsAFewTimesThenIsMissed() {
        AlarmState state = wake();
        for (int round = 1; round <= AlarmState.MAX_ROUNDS; round++) {
            state.fire(NOW + round * 10 * M);
            state.ringTimeout(NOW + round * 10 * M + AlarmState.RING_LIMIT);
        }
        assertEquals(AlarmState.MISSED, state.phase);
        AlarmState second = wake();
        second.fire(NOW);
        second.ringTimeout(NOW + AlarmState.RING_LIMIT);
        assertEquals(AlarmState.SNOOZED, second.phase);
        assertEquals(NOW + AlarmState.RING_LIMIT + AlarmState.AUTO_SNOOZE, second.next);
    }

    @Test
    public void plainAlarmIsDoneAfterTurningItOff() {
        AlarmState state = new AlarmState("reminder-bread", NOW, "Купить хлеб", false, true, false);
        state.fire(NOW);
        state.up(NOW + M);
        assertEquals(AlarmState.DONE, state.phase);
    }

    @Test
    public void syncKeepsProgressDropsRemovedAndDoesNotRingStaleMoments() {
        AlarmState ringing = wake();
        ringing.fire(NOW);
        AlarmState removed = new AlarmState("gone", NOW + 60 * M, "x", false, false, false);
        AlarmState local = new AlarmState("test-1", NOW + M, "test", false, false, true);
        List<AlarmState> current = new ArrayList<>(Arrays.asList(ringing, removed, local));
        AlarmState same = wake();
        AlarmState moved = new AlarmState("reminder-moved", NOW + 120 * M, "y", false, false, false);
        AlarmState stale = new AlarmState("reminder-old", NOW - 60 * M, "z", false, false, false);
        List<AlarmState> merged = AlarmState.merge(current, Arrays.asList(same, moved, stale), NOW);
        assertEquals(4, merged.size());
        assertEquals(AlarmState.RINGING, merged.get(0).phase);  // same key: progress kept
        assertEquals(AlarmState.MISSED, merged.get(2).phase);   // too old to ring now
        assertTrue(merged.contains(local));
        assertTrue(merged.stream().noneMatch(s -> s.id.equals("gone")));
        List<AlarmState> reloaded = AlarmState.listFromJson(AlarmState.listToJson(merged));
        assertEquals(merged.get(0).key(), reloaded.get(0).key());
        assertEquals(AlarmState.RINGING, reloaded.get(0).phase);
    }

    @Test
    public void serverPayloadsParse() throws Exception {
        AlarmState push = AlarmState.fromServer(new JSONObject()
                .put("id", "reminder-1").put("at", "2026-09-24T04:00:00+00:00").put("title", "Подъём")
                .put("wake_check", true).put("raise_volume", true));
        AlarmState listed = AlarmState.fromServer(new JSONObject()
                .put("id", "reminder-1").put("remind_at", "2026-09-24T04:00:00.000Z").put("title", "Подъём"));
        assertEquals(push.at, listed.at);
        assertEquals("2026-09-24T04:00:00Z", Iso.format(push.at));
        assertTrue(push.wakeCheck && push.raiseVolume);
    }

    @Test
    public void answersBecomeIdempotentSyncOperations() throws Exception {
        AlarmState state = wake();
        JSONObject up = new JSONArray(AlarmOps.up(state)).getJSONObject(0);
        assertEquals("reminder.ack", up.getString("type"));
        assertEquals("UP", up.getJSONObject("payload").getString("stage"));
        assertEquals("alarm-reminder-wake-" + NOW + "-UP", up.getString("op_id"));
        assertEquals(AlarmOps.up(state), AlarmOps.up(wake()));
        JSONObject awake = new JSONArray(AlarmOps.awake(state)).getJSONObject(0);
        assertEquals("AWAKE", awake.getJSONObject("payload").getString("stage"));
        JSONObject snooze = new JSONArray(AlarmOps.snooze(state, NOW + 10 * M + 1234)).getJSONObject(0);
        assertEquals("reminder.snooze", snooze.getString("type"));
        assertEquals(Iso.format(NOW + 10 * M), snooze.getJSONObject("payload").getString("until"));
        AlarmState plain = new AlarmState("r2", NOW, "x", false, false, false);
        assertEquals("reminder.done", new JSONArray(AlarmOps.up(plain)).getJSONObject(0).getString("type"));
        assertEquals(Collections.emptyList(), Collections.emptyList());
    }
}
