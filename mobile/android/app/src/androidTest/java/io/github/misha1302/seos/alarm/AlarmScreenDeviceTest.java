package io.github.misha1302.seos.alarm;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

import android.app.UiAutomation;
import android.content.Context;
import android.content.Intent;
import android.graphics.Rect;
import android.view.View;
import android.view.ViewGroup;
import android.widget.Button;
import androidx.test.core.app.ActivityScenario;
import androidx.test.core.app.ApplicationProvider;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import java.util.ArrayList;
import java.util.List;
import org.junit.After;
import org.junit.Before;
import org.junit.Test;
import org.junit.runner.RunWith;

/**
 * Every answer on the ringing medication alarm is fully on screen, held upright or
 * sideways. On a real phone in landscape «Не принял» used to be below the screen edge,
 * with nothing to scroll.
 */
@RunWith(AndroidJUnit4.class)
public class AlarmScreenDeviceTest {
    private static final String ID = "reminder-screen-med";
    private Context context;
    private UiAutomation automation;

    @Before
    public void setUp() {
        context = ApplicationProvider.getApplicationContext();
        automation = InstrumentationRegistry.getInstrumentation().getUiAutomation();
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
        AlarmStore.save(context, new ArrayList<>());
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
            InstrumentationRegistry.getInstrumentation().waitForIdleSync();
            scenario.onActivity(activity -> {
                List<Button> found = new ArrayList<>();
                buttons(activity.getWindow().getDecorView(), found);
                assertEquals("«Принял», «Отложить», «Не принял»", 3, found.size());
                for (Button button : found) {
                    Rect visible = new Rect();
                    boolean shown = button.getGlobalVisibleRect(visible);
                    assertTrue("«" + button.getText() + "» is on screen (rotation " + rotation + ")", shown);
                    assertEquals("«" + button.getText() + "» is not cut (rotation " + rotation + ")",
                            button.getHeight(), visible.height());
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
