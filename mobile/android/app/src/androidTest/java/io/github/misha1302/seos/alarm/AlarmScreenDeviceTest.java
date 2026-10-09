package io.github.misha1302.seos.alarm;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

import android.app.UiAutomation;
import android.content.Context;
import android.content.Intent;
import android.graphics.Rect;
import android.os.ParcelFileDescriptor;
import android.os.SystemClock;
import android.view.View;
import android.view.ViewGroup;
import android.widget.Button;
import androidx.core.graphics.Insets;
import androidx.core.view.ViewCompat;
import androidx.core.view.WindowInsetsCompat;
import androidx.test.core.app.ActivityScenario;
import androidx.test.core.app.ApplicationProvider;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import java.io.FileInputStream;
import java.io.IOException;
import java.util.ArrayList;
import java.util.List;
import java.util.function.BooleanSupplier;
import org.junit.After;
import org.junit.Before;
import org.junit.Test;
import org.junit.runner.RunWith;

/**
 * Every answer on the ringing medication alarm is fully on screen, held upright or
 * sideways. On a real phone in landscape «Не принял» used to be below the screen edge,
 * with nothing to scroll; then, with three-button navigation, the right answer ran under
 * the navigation bar standing on the side. The test switches to three-button navigation
 * (emulators default to gestures, whose thin bar hides nothing) and puts it back after.
 */
@RunWith(AndroidJUnit4.class)
public class AlarmScreenDeviceTest {
    private static final String ID = "reminder-screen-med";
    private static final String THREE_BUTTON = "com.android.internal.systemui.navbar.threebutton";
    private static final String GESTURAL = "com.android.internal.systemui.navbar.gestural";
    private Context context;
    private UiAutomation automation;
    private String navigationMode;

    @Before
    public void setUp() {
        context = ApplicationProvider.getApplicationContext();
        automation = InstrumentationRegistry.getInstrumentation().getUiAutomation();
        navigationMode = shell("settings get secure navigation_mode").trim();
        shell("cmd overlay enable-exclusive --category " + THREE_BUTTON);
        // The switch lands asynchronously; a screen laid out before it never sees the bar.
        waitFor("three-button navigation", () -> "0".equals(shell("settings get secure navigation_mode").trim()));
        AlarmStore.save(context, new ArrayList<>());
        AlarmState state = new AlarmState(ID, System.currentTimeMillis(), "Витамин D", false, false, true);
        state.checkinTemplate = "checkin-screen-med";
        state.checkinRecurrence = "2026-10-08T09:00:00";
        state.checkinKind = "MEDICATION";
        state.phase = AlarmState.RINGING;  // shown as ringing; the sound service is not started
        AlarmStore.put(context, state);
    }

    @After
    public void tearDown() {
        automation.setRotation(UiAutomation.ROTATION_UNFREEZE);
        if ("2".equals(navigationMode)) {
            shell("cmd overlay enable-exclusive --category " + GESTURAL);
            waitFor("gesture navigation back", () -> "2".equals(shell("settings get secure navigation_mode").trim()));
        }
        AlarmStore.save(context, new ArrayList<>());
    }

    private static void waitFor(String what, BooleanSupplier done) {
        long deadline = SystemClock.uptimeMillis() + 10_000L;
        while (!done.getAsBoolean()) {
            if (SystemClock.uptimeMillis() > deadline) throw new AssertionError("timed out waiting for " + what);
            SystemClock.sleep(100);
        }
    }

    private String shell(String command) {
        StringBuilder out = new StringBuilder();
        ParcelFileDescriptor fd = automation.executeShellCommand(command);
        try (FileInputStream in = new ParcelFileDescriptor.AutoCloseInputStream(fd)) {
            byte[] buffer = new byte[1024];
            for (int n; (n = in.read(buffer)) > 0; ) out.append(new String(buffer, 0, n));
        } catch (IOException e) {
            throw new AssertionError(command, e);
        }
        return out.toString();
    }

    private static void buttons(View view, List<Button> out) {
        if (view instanceof Button) out.add((Button) view);
        if (view instanceof ViewGroup) {
            ViewGroup group = (ViewGroup) view;
            for (int i = 0; i < group.getChildCount(); i++) buttons(group.getChildAt(i), out);
        }
    }

    private void assertEveryAnswerVisible(int rotation) {
        automation.setRotation(rotation);
        InstrumentationRegistry.getInstrumentation().waitForIdleSync();
        Intent intent = new Intent(context, AlarmActivity.class).putExtra(AlarmReceiver.EXTRA_ID, ID);
        try (ActivityScenario<AlarmActivity> scenario = ActivityScenario.launch(intent)) {
            // The display turns only once this screen is on top (a launcher may hold it upright),
            // and the rotation a previous test run's UiAutomation saved is restored when that
            // connection goes away, at any moment: ask again until this screen is turned.
            waitFor("the alarm laid out at rotation " + rotation, () -> {
                boolean[] turned = {false};
                scenario.onActivity(activity -> {
                    View decor = activity.getWindow().getDecorView();
                    turned[0] = decor.getDisplay() != null && decor.getDisplay().getRotation() == rotation
                            && decor.isLaidOut() && (decor.getWidth() > decor.getHeight()) == (rotation % 2 == 1);
                });
                if (!turned[0]) automation.setRotation(rotation);
                return turned[0];
            });
            InstrumentationRegistry.getInstrumentation().waitForIdleSync();
            scenario.onActivity(activity -> {
                View decor = activity.getWindow().getDecorView();
                Insets bars = ViewCompat.getRootWindowInsets(decor).getInsets(
                        WindowInsetsCompat.Type.systemBars() | WindowInsetsCompat.Type.displayCutout());
                Rect safe = new Rect(bars.left, bars.top, decor.getWidth() - bars.right, decor.getHeight() - bars.bottom);
                List<Button> found = new ArrayList<>();
                buttons(decor, found);
                assertEquals("«Принял», «Отложить», «Не принял»", 3, found.size());
                for (Button button : found) {
                    Rect visible = new Rect();
                    boolean shown = button.getGlobalVisibleRect(visible);
                    assertTrue("«" + button.getText() + "» is on screen (rotation " + rotation + ")", shown);
                    assertEquals("«" + button.getText() + "» is not cut (rotation " + rotation + ")",
                            button.getHeight(), visible.height());
                    int[] at = new int[2];
                    button.getLocationInWindow(at);
                    Rect bounds = new Rect(at[0], at[1], at[0] + button.getWidth(), at[1] + button.getHeight());
                    assertTrue("«" + button.getText() + "» " + bounds + " is clear of the system bars " + safe
                            + " (rotation " + rotation + ")", safe.contains(bounds));
                    assertTrue("«" + button.getText() + "» is a large target", button.getHeight() >= 48 * activity.getResources().getDisplayMetrics().density);
                    assertEquals("«" + button.getText() + "» shows its whole label", 0, button.getLayout().getEllipsisCount(button.getLineCount() - 1));
                }
            });
        }
    }

    @Test
    public void everyAnswerIsVisibleUpright() {
        assertEveryAnswerVisible(UiAutomation.ROTATION_FREEZE_0);
    }

    @Test
    public void everyAnswerIsVisibleSideways() {
        assertEveryAnswerVisible(UiAutomation.ROTATION_FREEZE_90);
    }
}
