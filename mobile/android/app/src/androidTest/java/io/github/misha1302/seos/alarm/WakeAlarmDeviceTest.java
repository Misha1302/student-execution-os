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
        assertEquals(1, AlarmSyncWorker.apply(context, list, owner));
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

        AlarmSyncWorker.apply(context, new JSONArray(), owner);  // deleted on another device
        assertTrue(AlarmStore.get(context, "reminder-device-wake") == null);
    }

    @Test
    public void logoutAndAccountSwitchRemoveThePreviousAccountsAlarms() throws Exception {
        long at = System.currentTimeMillis() + 3_600_000L;
        JSONArray list = new JSONArray().put(new JSONObject().put("id", "reminder-account-a")
                .put("remind_at", Iso.format(at)).put("title", "A").put("wake_check", false).put("raise_volume", false));
        String ownerA = owner;
        assertEquals(1, AlarmSyncWorker.apply(context, list, ownerA));
        AlarmState local = new AlarmState("seos-test-alarm", at, "Test", false, false, true);
        java.util.List<AlarmState> withTest = new ArrayList<>(AlarmStore.all(context));
        withTest.add(local);
        AlarmStore.save(context, withTest);

        // Logout: credentials go first, then the page clears the alarms.
        context.getSharedPreferences("CapacitorStorage", Context.MODE_PRIVATE).edit().remove("seos.token").commit();
        AlarmStore.clearAccountAlarms(context);
        assertNull(AlarmStore.get(context, "reminder-account-a"));
        assertTrue("the local test alarm stays", AlarmStore.get(context, "seos-test-alarm") != null);

        // A response fetched for account A arrives after account B signed in: dropped.
        signIn("account-b");
        assertEquals(0, AlarmSyncWorker.apply(context, list, ownerA));
        assertNull(AlarmStore.get(context, "reminder-account-a"));

        // Account A's alarms stored without a logout (e.g. an older app version) go too
        // when account B's list arrives.
        AlarmStore.setOwner(context, ownerA);
        AlarmStore.save(context, java.util.Collections.singletonList(
                new AlarmState("reminder-account-a", at, "A", false, false, false)));
        AlarmSyncWorker.apply(context, new JSONArray(), owner);
        assertNull(AlarmStore.get(context, "reminder-account-a"));
    }
}
