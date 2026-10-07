package io.github.misha1302.seos.alarm;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
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
    public void sameEpisodeUsesFreshServerMetadataAndCompatibleLocalProgress() {
        AlarmState old = new AlarmState("reminder-wake", NOW, "Old title", true, true, false);
        old.fire(NOW);
        AlarmState fresh = new AlarmState("reminder-wake", NOW, "New title", false, false, false);
        AlarmState merged = AlarmState.merge(Collections.singletonList(old), Collections.singletonList(fresh), NOW).get(0);
        assertEquals("New title", merged.title);
        assertFalse(merged.wakeCheck);
        assertFalse(merged.raiseVolume);
        assertEquals(AlarmState.RINGING, merged.phase);
        assertEquals(1, merged.round);
    }

    private static AlarmState listed(boolean acknowledged) throws Exception {
        return AlarmState.fromServer(new JSONObject().put("id", "reminder-wake").put("remind_at", Iso.format(NOW))
                .put("title", "Подъём").put("wake_check", true).put("raise_volume", true).put("status", "FIRED")
                .put("acknowledged_at", acknowledged ? Iso.format(NOW + M) : JSONObject.NULL));
    }

    @Test
    public void iAmUpOnAnotherPhoneStopsThisOneButKeepsTheAnsweringPhonesAwakeCheck() throws Exception {
        // Phone B is still ringing; phone A answered «Я встал» (the server lists the
        // wake alarm as FIRED + acknowledged until the awake check is answered).
        AlarmState ringingOnB = wake();
        ringingOnB.fire(NOW);
        AlarmState onB = AlarmState.merge(Collections.singletonList(ringingOnB),
                Collections.singletonList(listed(true)), NOW + 2 * M).get(0);
        assertEquals(AlarmState.DONE, onB.phase);
        assertFalse(onB.active());

        AlarmState answeredOnA = wake();
        answeredOnA.fire(NOW);
        answeredOnA.up(NOW + M);
        AlarmState onA = AlarmState.merge(Collections.singletonList(answeredOnA),
                Collections.singletonList(listed(true)), NOW + 2 * M).get(0);
        assertEquals(AlarmState.AWAKE_WAIT, onA.phase);
        assertEquals(AlarmState.AWAKE_CHECK, onA.nextKind);

        // A phone that first hears of the alarm after it was answered never rings it.
        AlarmState fresh = AlarmState.merge(Collections.emptyList(), Collections.singletonList(listed(true)), NOW).get(0);
        assertEquals(AlarmState.DONE, fresh.phase);
        // Not yet answered anywhere: ringing continues.
        AlarmState still = AlarmState.merge(Collections.singletonList(ringingOnB),
                Collections.singletonList(listed(false)), NOW).get(0);
        assertEquals(AlarmState.RINGING, still.phase);
        // The acknowledgement survives the phone's own store.
        assertTrue(AlarmState.listFromJson(AlarmState.listToJson(Collections.singletonList(onB))).get(0).acknowledged);
    }

    @Test
    public void removedAlarmDoesNotKeepRinging() {
        AlarmState ringing = wake();
        ringing.fire(NOW);
        assertTrue(AlarmState.merge(Collections.singletonList(ringing), Collections.emptyList(), NOW).isEmpty());
    }

    @Test
    public void changedTimeStartsNewEpisodeAndLogoutPreservesOnlyLocalTestAlarm() {
        AlarmState old = wake();
        old.fire(NOW);
        AlarmState moved = new AlarmState(old.id, NOW + 60 * M, "Moved", true, false, false);
        AlarmState local = new AlarmState("test", NOW + M, "Test", false, false, true);
        List<AlarmState> merged = AlarmState.merge(Arrays.asList(old, local), Collections.singletonList(moved), NOW);
        assertEquals(2, merged.size());
        AlarmState episode = merged.stream().filter(s -> !s.local).findFirst().get();
        assertEquals(AlarmState.SCHEDULED, episode.phase);
        assertEquals(0, episode.round);
        assertEquals(Collections.singletonList(local), AlarmState.withoutAccountAlarms(merged));
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

    private static AlarmState medicationPrompt() throws Exception {
        return AlarmState.fromServer(new JSONObject().put("id", "reminder-checkin-vit").put("remind_at", Iso.format(NOW))
                .put("title", "Витамин D").put("status", "SCHEDULED")
                .put("checkin", new JSONObject().put("template_id", "checkin-vit")
                        .put("original_recurrence_id", "2026-10-07T09:00:00").put("kind", "MEDICATION")));
    }

    @Test
    public void aCheckinPromptCarriesItsOccurrenceAndSurvivesStorage() throws Exception {
        AlarmState state = medicationPrompt();
        assertTrue(state.answersCheckin());
        assertTrue(state.isMedication());
        AlarmState stored = AlarmState.fromJson(state.toJson());
        assertEquals("checkin-vit", stored.checkinTemplate);
        assertEquals("2026-10-07T09:00:00", stored.checkinRecurrence);
        assertTrue(stored.answersCheckin());
        // Stored by an older app version (no check-in fields): an ordinary reminder.
        JSONObject legacy = new JSONObject().put("id", "r-old").put("at", NOW).put("title", "x").put("wake", false)
                .put("loud", false).put("local", false).put("ack", false).put("phase", AlarmState.SCHEDULED)
                .put("round", 0).put("next", NOW).put("nextKind", AlarmState.FIRE);
        AlarmState old = AlarmState.fromJson(legacy);
        assertFalse(old.answersCheckin());
        assertEquals("", old.checkinTemplate);
        // A quota needs a count, not a button on the alarm.
        AlarmState quota = AlarmState.fromServer(new JSONObject().put("id", "r-q").put("remind_at", Iso.format(NOW))
                .put("checkin", new JSONObject().put("template_id", "q").put("original_recurrence_id", "2026-10-07T09:00:00")
                        .put("kind", "QUOTA")));
        assertFalse(quota.answersCheckin());
    }

    @Test
    public void takenOnTheAlarmIsTheOccurrencesOutcomeAtThePressAndSilencingIsNot() throws Exception {
        AlarmState state = medicationPrompt();
        state.fire(NOW);
        long pressed = NOW + 3 * M + 17_000L;
        JSONObject taken = new JSONArray(AlarmOps.checkin(state, true, pressed)).getJSONObject(0);
        assertEquals("checkin.occurrence.done", taken.getString("type"));
        assertEquals("checkin-vit", taken.getString("entity_id"));
        JSONObject payload = taken.getJSONObject("payload");
        assertEquals("2026-10-07T09:00:00", payload.getString("original_recurrence_id"));
        assertEquals(Iso.format(pressed), payload.getString("occurred_at"));
        // Fixed by the alarm and the answer: a retry or a second tap is one operation.
        assertEquals(taken.getString("op_id"),
                new JSONArray(AlarmOps.checkin(state, true, pressed + 60_000L)).getJSONObject(0).getString("op_id"));
        JSONObject notTaken = new JSONArray(AlarmOps.checkin(state, false, pressed)).getJSONObject(0);
        assertEquals("checkin.occurrence.skip", notTaken.getString("type"));
        assertFalse(notTaken.getJSONObject("payload").has("occurred_at"));
        assertTrue(!taken.getString("op_id").equals(notTaken.getString("op_id")));
        // Turning the alarm off is a reminder operation, never an outcome.
        assertEquals("reminder.done", new JSONArray(AlarmOps.up(state)).getJSONObject(0).getString("type"));
        state.answered();
        assertEquals(AlarmState.DONE, state.phase);
        assertFalse(state.active());
    }

    @Test
    public void aSyncWithoutPromptsKeepsTheCheckinAlarmsAndTheirProgress() throws Exception {
        AlarmState prompt = medicationPrompt();
        prompt.fire(NOW);
        prompt.answered();  // answered here, not yet delivered to the server
        AlarmState plain = new AlarmState("r-plain", NOW + 60 * M, "Созвон", false, false, false);
        List<AlarmState> current = Arrays.asList(prompt, plain);
        List<AlarmState> kept = AlarmState.checkinPromptsOf(current);
        assertEquals(1, kept.size());
        List<AlarmState> incoming = new ArrayList<>(Collections.singletonList(
                new AlarmState("r-plain", NOW + 60 * M, "Созвон", false, false, false)));
        incoming.addAll(kept);
        List<AlarmState> merged = AlarmState.merge(current, incoming, NOW + M);
        AlarmState after = null;
        for (AlarmState state : merged) if (state.id.equals(prompt.id)) after = state;
        assertTrue("the medication alarm is still known to the phone", after != null);
        assertEquals("an answer given on the alarm is not undone by a page sync", AlarmState.DONE, after.phase);
        assertEquals("checkin-vit", after.checkinTemplate);
    }
}
