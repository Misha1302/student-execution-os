package io.github.misha1302.seos.alarm;

import android.app.Activity;
import android.app.KeyguardManager;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.graphics.Color;
import android.graphics.Typeface;
import android.os.Build;
import android.os.Bundle;
import android.view.Gravity;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import androidx.core.content.ContextCompat;
import java.text.DateFormat;
import java.util.Date;

/**
 * The ringing alarm on top of the lock screen: big time, the title, large buttons.
 * Every answer stays on screen in any orientation: on a short (landscape) screen the
 * buttons sit side by side under a smaller clock, and the whole screen scrolls when a
 * large system font still does not fit. Leaving it (Back, Home) does not silence the alarm: the sound belongs to {@link
 * AlarmService} and only an answer stops it.
 */
public class AlarmActivity extends Activity {
    static final String ACTION_CLOSED = "io.github.misha1302.seos.ALARM_CLOSED";
    private final BroadcastReceiver closed = new BroadcastReceiver() {
        @Override
        public void onReceive(Context context, Intent intent) {
            finish();
        }
    };

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O_MR1) {
            setShowWhenLocked(true);
            setTurnScreenOn(true);
            KeyguardManager keyguard = getSystemService(KeyguardManager.class);
            if (keyguard != null) keyguard.requestDismissKeyguard(this, null);
        } else {
            getWindow().addFlags(WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED | WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON);
        }
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        ContextCompat.registerReceiver(this, closed, new IntentFilter(ACTION_CLOSED), ContextCompat.RECEIVER_NOT_EXPORTED);
        render();
    }

    private void render() {
        String id = getIntent().getStringExtra(AlarmReceiver.EXTRA_ID);
        AlarmState state = id == null ? null : AlarmStore.get(this, id);
        if (state == null || !AlarmState.RINGING.equals(state.phase)) {
            finish();
            return;
        }
        float density = getResources().getDisplayMetrics().density;
        // A phone held sideways is ~400dp tall: a 72sp clock and three stacked 72dp buttons
        // pushed «Не принял» off the screen (seen on a real device).
        boolean compact = getResources().getConfiguration().screenHeightDp < 560;
        int pad = (int) ((compact ? 16 : 32) * density);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setGravity(Gravity.CENTER);
        root.setPadding(pad, pad, pad, pad);

        TextView time = new TextView(this);
        time.setText(DateFormat.getTimeInstance(DateFormat.SHORT).format(new Date()));
        time.setTextColor(Color.WHITE);
        time.setTextSize(compact ? 48 : 72);
        time.setTypeface(Typeface.DEFAULT_BOLD);
        time.setGravity(Gravity.CENTER);
        root.addView(time);

        TextView title = new TextView(this);
        title.setText(state.title);
        title.setTextColor(Color.rgb(210, 214, 220));
        title.setTextSize(compact ? 20 : 24);
        title.setGravity(Gravity.CENTER);
        title.setPadding(0, pad / 2, 0, compact ? pad / 2 : pad * 2);
        root.addView(title);

        LinearLayout buttons = new LinearLayout(this);
        buttons.setOrientation(compact ? LinearLayout.HORIZONTAL : LinearLayout.VERTICAL);
        root.addView(buttons, new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT));
        if (state.answersCheckin()) {
            // A check-in prompt is answered right here; silencing it is not an answer, so
            // there is no plain «Выключить»: «Отложить» or a real outcome stops the sound.
            boolean med = state.isMedication();
            buttons.addView(button(AlarmStore.label(this, med ? "alarm_taken" : "alarm_checkin_done"), Color.rgb(46, 125, 50),
                    compact, () -> answer(AlarmReceiver.ACTION_CHECKIN_DONE, state.id)));
            buttons.addView(button(AlarmStore.label(this, "alarm_snooze"), Color.rgb(60, 66, 76),
                    compact, () -> answer(AlarmReceiver.ACTION_SNOOZE, state.id)));
            buttons.addView(button(AlarmStore.label(this, med ? "alarm_not_taken" : "alarm_checkin_skip"), Color.rgb(90, 52, 52),
                    compact, () -> answer(AlarmReceiver.ACTION_CHECKIN_SKIP, state.id)));
        } else {
            buttons.addView(button(AlarmStore.label(this, state.wakeCheck ? "alarm_up" : "alarm_done"), Color.rgb(46, 125, 50),
                    compact, () -> answer(AlarmReceiver.ACTION_UP, state.id)));
            buttons.addView(button(AlarmStore.label(this, "alarm_snooze"), Color.rgb(60, 66, 76),
                    compact, () -> answer(AlarmReceiver.ACTION_SNOOZE, state.id)));
        }
        ScrollView scroll = new ScrollView(this);
        scroll.setFillViewport(true);  // centred when it fits, scrollable when it does not
        scroll.setBackgroundColor(Color.rgb(15, 18, 22));
        scroll.addView(root, new ScrollView.LayoutParams(ScrollView.LayoutParams.MATCH_PARENT,
                ScrollView.LayoutParams.WRAP_CONTENT));
        setContentView(scroll);
    }

    private Button button(String text, int color, boolean compact, Runnable action) {
        float density = getResources().getDisplayMetrics().density;
        Button button = new Button(this);
        button.setText(text);
        button.setTextSize(compact ? 18 : 22);
        button.setTextColor(Color.WHITE);
        button.setBackgroundColor(color);
        button.setAllCaps(false);
        // A minimum, not a fixed height: a long label or a large font wraps instead of being cut.
        button.setMinHeight((int) ((compact ? 64 : 72) * density));
        button.setMinimumHeight((int) ((compact ? 64 : 72) * density));
        int gap = (int) ((compact ? 8 : 16) * density);
        LinearLayout.LayoutParams params = compact
                ? new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f)
                : new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        if (compact) {
            params.leftMargin = gap / 2;
            params.rightMargin = gap / 2;
        } else {
            params.topMargin = gap;
        }
        button.setLayoutParams(params);
        button.setOnClickListener(v -> action.run());
        return button;
    }

    private void answer(String action, String id) {
        AlarmReceiver.handle(this, action, id, null);
        finish();
    }

    @Override
    protected void onDestroy() {
        try {
            unregisterReceiver(closed);
        } catch (IllegalArgumentException ignored) {
            // not registered
        }
        super.onDestroy();
    }
}
