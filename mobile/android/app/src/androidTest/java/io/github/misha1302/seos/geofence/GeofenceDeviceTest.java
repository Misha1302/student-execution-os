package io.github.misha1302.seos.geofence;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

import android.content.Context;
import android.content.Intent;
import android.location.Criteria;
import android.location.Location;
import android.location.LocationManager;
import android.os.ParcelFileDescriptor;
import android.os.SystemClock;
import androidx.test.core.app.ApplicationProvider;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import androidx.work.WorkInfo;
import androidx.work.WorkManager;
import io.github.misha1302.seos.alarm.AlarmSyncWorker;
import io.github.misha1302.seos.reminders.ReminderActionWorker;
import java.io.FileInputStream;
import java.io.InputStream;
import java.util.Collections;
import java.util.List;
import org.junit.After;
import org.junit.Assume;
import org.junit.Before;
import org.junit.Test;
import org.junit.runner.RunWith;

/**
 * Real device check of place reminders: the platform's own proximity alert fires the
 * receiver when a (mock) position crosses into the trigger's area, the phone records it
 * once, queues the firing for the server, and a second crossing of a one-shot trigger
 * does nothing, and another account's trigger never fires. No server is needed: the
 * queued sync operation is checked in WorkManager, scoped to the trigger's own tag so
 * jobs other device tests queued (same worker, same shared tag) are never counted. (Revoking a runtime permission kills
 * the app process, so the denied path is covered by the registrar's permission check.)
 */
@RunWith(AndroidJUnit4.class)
public class GeofenceDeviceTest {
    private static final double LAT = 55.7512;
    private static final double LON = 37.6184;
    private Context context;
    private LocationManager manager;
    private String provider;

    private static void shell(String command) throws Exception {
        ParcelFileDescriptor out = InstrumentationRegistry.getInstrumentation().getUiAutomation().executeShellCommand(command);
        try (InputStream in = new FileInputStream(out.getFileDescriptor())) {
            byte[] buffer = new byte[1024];
            while (in.read(buffer) > 0) { /* drain */ }
        }
    }

    @Before
    public void setUp() throws Exception {
        context = ApplicationProvider.getApplicationContext();
        String pkg = context.getPackageName();
        shell("pm grant " + pkg + " android.permission.ACCESS_FINE_LOCATION");
        shell("pm grant " + pkg + " android.permission.ACCESS_COARSE_LOCATION");
        shell("pm grant " + pkg + " android.permission.ACCESS_BACKGROUND_LOCATION");
        shell("pm grant " + pkg + " android.permission.POST_NOTIFICATIONS");
        shell("appops set " + pkg + " android:mock_location allow");
        shell("settings put secure location_mode 3");
        // A signed-in session as the web client writes it (the receiver checks ownership).
        context.getSharedPreferences("CapacitorStorage", Context.MODE_PRIVATE).edit()
                .putString("seos.server", "http://127.0.0.1:9").putString("seos.user", "{\"account_id\":\"geo-device\"}")
                .putString("seos.token", "geo-device-token").commit();
        manager = context.getSystemService(LocationManager.class);
        provider = LocationManager.GPS_PROVIDER;
        try {
            manager.removeTestProvider(provider);
        } catch (IllegalArgumentException | SecurityException none) { /* not added yet */ }
        manager.addTestProvider(provider, false, false, false, false, true, true, true,
                android.location.provider.ProviderProperties.POWER_USAGE_LOW,
                android.location.provider.ProviderProperties.ACCURACY_FINE);
        manager.setTestProviderEnabled(provider, true);
        GeofenceRegistrar.clear(context);
        // Earlier device tests (the wake alarm's «Я встал», notification buttons) queue
        // ReminderActionWorker jobs too. Cancel what is pending and drop finished records
        // so nothing from another test is in the database; the assertions below are in
        // any case scoped to the trigger under test.
        WorkManager work = WorkManager.getInstance(context);
        work.cancelAllWork().getResult().get();
        work.pruneWork().getResult().get();
        move(LAT + 0.05, LON);  // ~5.5 km away
    }

    @After
    public void tearDown() {
        GeofenceRegistrar.clear(context);
        try {
            manager.removeTestProvider(provider);
        } catch (IllegalArgumentException | SecurityException ignored) { /* best effort */ }
    }

    private void move(double lat, double lon) {
        Location location = new Location(provider);
        location.setLatitude(lat);
        location.setLongitude(lon);
        location.setAccuracy(5f);
        location.setTime(System.currentTimeMillis());
        location.setElapsedRealtimeNanos(SystemClock.elapsedRealtimeNanos());
        manager.setTestProviderLocation(provider, location);
    }

    private boolean waitFired(String id, long millis) throws InterruptedException {
        long until = SystemClock.elapsedRealtime() + millis;
        while (SystemClock.elapsedRealtime() < until) {
            if (GeofenceStore.lastFired(context, id) > 0) return true;
            move(LAT + 0.0002 * Math.random(), LON);  // keep feeding fixes inside the area
            Thread.sleep(1000);
        }
        return false;
    }

    @Test
    public void enteringThePlaceFiresOnceAndQueuesTheOperation() throws Exception {
        String owner = AlarmSyncWorker.sessionOwnerOf(context);
        Assume.assumeTrue("no session owner", owner != null);
        GeofenceStore.setOwner(context, owner);
        GeofenceSpec home = new GeofenceSpec("trigger-device-home", "Разобрать вещи", "ENTER", LAT, LON, 200f, false, "Дом");
        assertEquals(1, GeofenceRegistrar.apply(context, Collections.singletonList(home)));
        Thread.sleep(3000);
        move(LAT, LON);
        assertTrue("the platform proximity alert did not fire", waitFired(home.id, 90_000));
        // One-shot: the phone stops watching it; a second crossing changes nothing.
        assertEquals(0, GeofenceStore.all(context).size());
        long first = GeofenceStore.lastFired(context, home.id);
        move(LAT + 0.05, LON);
        Thread.sleep(3000);
        move(LAT, LON);
        Thread.sleep(5000);
        assertEquals(first, GeofenceStore.lastFired(context, home.id));
        assertEquals("the firing is queued once for the server", 1, queuedFor(home.id));
    }

    /** Sync jobs this trigger queued, across every ten-minute bucket and retry. */
    private int queuedFor(String triggerId) throws Exception {
        List<WorkInfo> work = WorkManager.getInstance(context).getWorkInfosByTag(GeofenceReceiver.sourceTag(triggerId)).get();
        int queued = 0;
        for (WorkInfo info : work) {
            assertTrue("a place trigger only queues the shared sync action", info.getTags().contains(ReminderActionWorker.QUEUE_TAG));
            if (info.getState() != WorkInfo.State.CANCELLED) queued++;
        }
        return queued;
    }

    private Intent crossing(String triggerId, boolean entering) {
        return new Intent(context, GeofenceReceiver.class)
                .putExtra(GeofenceReceiver.EXTRA_TRIGGER, triggerId)
                .putExtra(LocationManager.KEY_PROXIMITY_ENTERING, entering);
    }

    /**
     * The platform may deliver one crossing twice, or deliver the other direction. Fed
     * straight into the receiver (no location timing involved): a one-shot trigger fires
     * and queues exactly once, a doubled callback adds nothing, and the opposite
     * transition of an EXIT trigger is ignored.
     */
    @Test
    public void aDoubledCallbackOrTheWrongTransitionQueuesNothingMore() throws Exception {
        String owner = AlarmSyncWorker.sessionOwnerOf(context);
        Assume.assumeTrue("no session owner", owner != null);
        GeofenceStore.setOwner(context, owner);
        GeofenceSpec once = new GeofenceSpec("trigger-device-once", "Купить хлеб", "ENTER", LAT, LON, 200f, false, "Магазин");
        GeofenceSpec leave = new GeofenceSpec("trigger-device-leave", "Выключить свет", "EXIT", LAT, LON, 200f, true, "Дом");
        GeofenceStore.save(context, java.util.Arrays.asList(once, leave));
        GeofenceReceiver receiver = new GeofenceReceiver();

        receiver.onReceive(context, crossing(once.id, true));
        long first = GeofenceStore.lastFired(context, once.id);
        assertTrue("the first crossing fires", first > 0);
        receiver.onReceive(context, crossing(once.id, true));
        assertEquals(first, GeofenceStore.lastFired(context, once.id));
        assertEquals("a doubled callback is one queued operation", 1, queuedFor(once.id));

        receiver.onReceive(context, crossing(leave.id, true));  // entering an EXIT trigger
        assertEquals(0, GeofenceStore.lastFired(context, leave.id));
        assertEquals(0, queuedFor(leave.id));
        receiver.onReceive(context, crossing(leave.id, false));
        receiver.onReceive(context, crossing(leave.id, false));  // inside the repeat cool-down
        assertEquals("a repeating trigger waits for its cool-down", 1, queuedFor(leave.id));
    }

    @Test
    public void anotherAccountsTriggerNeverFires() throws Exception {
        GeofenceStore.setOwner(context, "http://other\naccount:someone-else");
        GeofenceSpec spec = new GeofenceSpec("trigger-other", "Чужое", "ENTER", LAT, LON, 200f, false, "Дом");
        GeofenceStore.save(context, Collections.singletonList(spec));
        GeofenceRegistrar.registerAll(context);
        Thread.sleep(3000);
        move(LAT, LON);
        Thread.sleep(20_000);
        assertEquals(0, GeofenceStore.lastFired(context, spec.id));
    }
}
