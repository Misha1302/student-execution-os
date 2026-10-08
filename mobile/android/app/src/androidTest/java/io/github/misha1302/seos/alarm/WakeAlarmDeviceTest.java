package io.github.misha1302.seos.alarm;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;

import android.app.ActivityManager;
import android.app.AlarmManager;
import android.content.Context;
import android.media.AudioManager;
import android.os.ParcelFileDescriptor;
import androidx.test.core.app.ApplicationProvider;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import java.io.FileInputStream;
import java.io.IOException;
import java.util.ArrayList;
import androidx.work.WorkInfo;
import androidx.work.WorkManager;
import io.github.misha1302.seos.storage.SessionCredentials;
import org.json.JSONArray;
import org.json.JSONObject;
import org.junit.After;
import org.junit.Before;
import org.junit.Test;
import org.junit.runner.RunWith;

/**
 * The wake alarm on a real device/emulator: the alarm clock is registered with
 * AlarmManager, firing starts the ringing foreground service with the alarm volume
 * raised, «Я встал» stops it, restores the volume and schedules the awake check, and a
 * server sync that no longer lists an alarm removes it.
 */
@RunWith(AndroidJUnit4.class)
public class WakeAlarmDeviceTest {
    private Context context;
    private AudioManager audio;
    private int originalVolume;
    private String owner;

    private void signIn(String account) {
        context.getSharedPreferences("CapacitorStorage", Context.MODE_PRIVATE).edit()
                .putString("seos.server", "https://seos.test").putString("seos.token", "token-" + account)
                .putString("seos.user", "{\"account_id\":\"" + account + "\"}").commit();
        owner = AlarmSyncWorker.sessionOwner(context);
    }

    @Before
    public void setUp() throws Exception {
        context = ApplicationProvider.getApplicationContext();
        audio = context.getSystemService(AudioManager.class);
        shell("appops set " + context.getPackageName() + " SCHEDULE_EXACT_ALARM allow");
        AlarmStore.save(context, new ArrayList<>());
        signIn("account-a");
        audio.setStreamVolume(AudioManager.STREAM_ALARM, 1, 0);
        originalVolume = audio.getStreamVolume(AudioManager.STREAM_ALARM);
    }

    @After
    public void tearDown() {
        AlarmStore.save(context, new ArrayList<>());
        AlarmStore.setOwner(context, null);
        AlarmScheduler.rescheduleAll(context);
        context.getSharedPreferences("CapacitorStorage", Context.MODE_PRIVATE).edit().clear().commit();
        SessionCredentials.of(context).clear();
    }

    private static void shell(String command) throws IOException {
        ParcelFileDescriptor pfd = InstrumentationRegistry.getInstrumentation().getUiAutomation().executeShellCommand(command);
        try (FileInputStream in = new FileInputStream(pfd.getFileDescriptor())) {
            while (in.read(new byte[1024]) > 0) { /* drain */ }
        }
    }

    private boolean serviceRunning() {
        ActivityManager manager = context.getSystemService(ActivityManager.class);
        for (ActivityManager.RunningServiceInfo info : manager.getRunningServices(50)) {
            if (info.service.getClassName().equals(AlarmService.class.getName())) return true;
        }
        return false;
    }

    private static void waitFor(java.util.concurrent.Callable<Boolean> condition) throws Exception {
        for (int i = 0; i < 50 && !condition.call(); i++) Thread.sleep(100);
    }

    @Test
    public void wakeAlarmRingsLoudStopsOnIAmUpAndSchedulesTheAwakeCheck() throws Exception {
        long at = System.currentTimeMillis() + 3_600_000L;
        JSONArray list = new JSONArray().put(new JSONObject().put("id", "reminder-device-wake")
                .put("remind_at", Iso.format(at)).put("title", "Подъём").put("wake_check", true).put("raise_volume", true));
        assertEquals(1, AlarmSyncWorker.apply(context, list, owner, true));
        AlarmManager alarms = context.getSystemService(AlarmManager.class);
        assertTrue("an alarm clock is registered", alarms.getNextAlarmClock() != null);

        AlarmState state = AlarmStore.get(context, "reminder-device-wake");
        AlarmReceiver.ring(context, state, System.currentTimeMillis());
        waitFor(this::serviceRunning);
        assertTrue("the ringing service runs", serviceRunning());
        waitFor(() -> audio.getStreamVolume(AudioManager.STREAM_ALARM) == audio.getStreamMaxVolume(AudioManager.STREAM_ALARM));
        assertEquals(audio.getStreamMaxVolume(AudioManager.STREAM_ALARM), audio.getStreamVolume(AudioManager.STREAM_ALARM));

        AlarmReceiver.handle(context, AlarmReceiver.ACTION_UP, "reminder-device-wake", null);
        waitFor(() -> !serviceRunning());
        assertTrue("«Я встал» stops the sound", !serviceRunning());
        waitFor(() -> audio.getStreamVolume(AudioManager.STREAM_ALARM) == originalVolume);
        assertEquals("the volume is restored", originalVolume, audio.getStreamVolume(AudioManager.STREAM_ALARM));
        AlarmState after = AlarmStore.get(context, "reminder-device-wake");
        assertEquals(AlarmState.AWAKE_WAIT, after.phase);
        assertEquals(AlarmState.AWAKE_CHECK, after.nextKind);

        AlarmSyncWorker.apply(context, new JSONArray(), owner, true);  // deleted on another device
        assertTrue(AlarmStore.get(context, "reminder-device-wake") == null);
    }

    /**
     * An alarm answered or removed between firing and the ringing service starting (from
     * another device, by a sync): that start ends quietly instead of killing the app for
     * never going foreground, and an alarm that is ringing keeps ringing.
     */
    @Test
    public void aStartForAnAlarmNoLongerRingingEndsQuietlyAndLeavesTheRingingOneAlone() throws Exception {
        AlarmService.start(context, "reminder-already-answered");  // nothing ringing
        long at = System.currentTimeMillis() + 3_600_000L;
        JSONArray list = new JSONArray().put(new JSONObject().put("id", "reminder-device-ringing")
                .put("remind_at", Iso.format(at)).put("title", "Подъём").put("wake_check", false).put("raise_volume", false));
        assertEquals(1, AlarmSyncWorker.apply(context, list, owner, true));
        AlarmReceiver.ring(context, AlarmStore.get(context, "reminder-device-ringing"), System.currentTimeMillis());
        waitFor(this::serviceRunning);
        assertTrue("the next alarm rings", serviceRunning());

        AlarmService.start(context, "reminder-already-answered");  // a stale start while one rings
        waitFor(() -> !serviceRunning());
        assertTrue("a stale start does not silence the ringing alarm", serviceRunning());

        AlarmReceiver.handle(context, AlarmReceiver.ACTION_UP, "reminder-device-ringing", null);
        waitFor(() -> !serviceRunning());
        assertTrue("its own answer stops it", !serviceRunning());
    }

    @Test
    public void logoutAndAccountSwitchRemoveThePreviousAccountsAlarms() throws Exception {
        long at = System.currentTimeMillis() + 3_600_000L;
        JSONArray list = new JSONArray().put(new JSONObject().put("id", "reminder-account-a")
                .put("remind_at", Iso.format(at)).put("title", "A").put("wake_check", false).put("raise_volume", false));
        String ownerA = owner;
        assertEquals(1, AlarmSyncWorker.apply(context, list, ownerA, true));
        AlarmState local = new AlarmState("seos-test-alarm", at, "Test", false, false, true);
        java.util.List<AlarmState> withTest = new ArrayList<>(AlarmStore.all(context));
        withTest.add(local);
        AlarmStore.save(context, withTest);

        // Logout: credentials go first (secure and legacy locations), then the page clears the alarms.
        SessionCredentials.of(context).clear();
        AlarmStore.clearAccountAlarms(context);
        assertNull(AlarmStore.get(context, "reminder-account-a"));
        assertTrue("the local test alarm stays", AlarmStore.get(context, "seos-test-alarm") != null);

        // A response fetched for account A arrives after account B signed in: dropped.
        signIn("account-b");
        assertEquals(0, AlarmSyncWorker.apply(context, list, ownerA, true));
        assertNull(AlarmStore.get(context, "reminder-account-a"));

        // Account A's alarms stored without a logout (e.g. an older app version) go too
        // when account B's list arrives.
        AlarmStore.setOwner(context, ownerA);
        AlarmStore.save(context, java.util.Collections.singletonList(
                new AlarmState("reminder-account-a", at, "A", false, false, false)));
        AlarmSyncWorker.apply(context, new JSONArray(), owner, true);
        assertNull(AlarmStore.get(context, "reminder-account-a"));
    }

    private int queuedFor(String alarmId) throws Exception {
        int queued = 0;
        for (WorkInfo info : WorkManager.getInstance(context).getWorkInfosByTag("seos-alarm:" + alarmId).get()) {
            if (info.getState() != WorkInfo.State.CANCELLED) queued++;
        }
        return queued;
    }

    /**
     * A medication prompt that rings as an alarm: the page's own sync (which does not carry
     * check-in prompts) leaves it scheduled, «Принял» on the alarm stops the sound and
     * queues exactly one outcome for that occurrence, a second press adds nothing, and
     * silencing is not offered as an answer.
     */
    @Test
    public void medicationAlarmIsAnsweredOnTheAlarmOnceAndSurvivesAPageSync() throws Exception {
        WorkManager.getInstance(context).cancelAllWork().getResult().get();
        WorkManager.getInstance(context).pruneWork().getResult().get();
        long at = System.currentTimeMillis() + 3_600_000L;
        JSONObject prompt = new JSONObject().put("id", "reminder-device-med").put("remind_at", Iso.format(at))
                .put("title", "Витамин D").put("status", "SCHEDULED")
                .put("checkin", new JSONObject().put("template_id", "checkin-device-med")
                        .put("original_recurrence_id", "2026-10-07T09:00:00").put("kind", "MEDICATION"));
        assertEquals(1, AlarmSyncWorker.apply(context, new JSONArray().put(prompt), owner, true));
        // The page syncs only its standalone reminders: the medication alarm stays.
        AlarmSyncWorker.apply(context, new JSONArray(), owner, false);
        AlarmState state = AlarmStore.get(context, "reminder-device-med");
        assertTrue("a page sync without prompts keeps the medication alarm", state != null && state.answersCheckin());

        AlarmReceiver.ring(context, state, System.currentTimeMillis());
        waitFor(this::serviceRunning);
        assertTrue(serviceRunning());
        AlarmReceiver.handle(context, AlarmReceiver.ACTION_CHECKIN_DONE, "reminder-device-med", null);
        waitFor(() -> !serviceRunning());
        assertTrue("«Принял» stops the sound", !serviceRunning());
        assertEquals(AlarmState.DONE, AlarmStore.get(context, "reminder-device-med").phase);
        assertEquals("one outcome is queued for the server", 1, queuedFor("reminder-device-med"));
        AlarmReceiver.handle(context, AlarmReceiver.ACTION_CHECKIN_DONE, "reminder-device-med", null);
        AlarmReceiver.handle(context, AlarmReceiver.ACTION_CHECKIN_SKIP, "reminder-device-med", null);
        assertEquals("a second press is not a second outcome", 1, queuedFor("reminder-device-med"));
        // The server still lists the prompt until the outcome arrives: the answer here holds.
        AlarmSyncWorker.apply(context, new JSONArray().put(prompt), owner, true);
        assertEquals(AlarmState.DONE, AlarmStore.get(context, "reminder-device-med").phase);
    }
}
