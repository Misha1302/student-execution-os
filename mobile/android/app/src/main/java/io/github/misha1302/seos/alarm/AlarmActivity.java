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
import android.widget.TextView;
import androidx.core.content.ContextCompat;
import java.text.DateFormat;
import java.util.Date;

/**
 * The ringing alarm on top of the lock screen: big time, the title, two large buttons.
 * Leaving it (Back, Home) does not silence the alarm: the sound belongs to {@link
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
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setGravity(Gravity.CENTER);
        root.setBackgroundColor(Color.rgb(15, 18, 22));
        int pad = (int) (32 * getResources().getDisplayMetrics().density);
        root.setPadding(pad, pad, pad, pad);

        TextView time = new TextView(this);
        time.setText(DateFormat.getTimeInstance(DateFormat.SHORT).format(new Date()));
        time.setTextColor(Color.WHITE);
        time.setTextSize(72);
        time.setTypeface(Typeface.DEFAULT_BOLD);
        time.setGravity(Gravity.CENTER);
        root.addView(time);

        TextView title = new TextView(this);
        title.setText(state.title);
        title.setTextColor(Color.rgb(210, 214, 220));
        title.setTextSize(24);
        title.setGravity(Gravity.CENTER);
        title.setPadding(0, pad / 2, 0, pad * 2);
        root.addView(title);

        root.addView(button(AlarmStore.label(this, state.wakeCheck ? "alarm_up" : "alarm_done"), Color.rgb(46, 125, 50),
                () -> answer(AlarmReceiver.ACTION_UP, state.id)));
        root.addView(button(AlarmStore.label(this, "alarm_snooze"), Color.rgb(60, 66, 76),
                () -> answer(AlarmReceiver.ACTION_SNOOZE, state.id)));
        setContentView(root);
    }

    private Button button(String text, int color, Runnable action) {
        Button button = new Button(this);
        button.setText(text);
        button.setTextSize(22);
        button.setTextColor(Color.WHITE);
        button.setBackgroundColor(color);
        button.setAllCaps(false);
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT,
                (int) (72 * getResources().getDisplayMetrics().density));
        params.topMargin = (int) (16 * getResources().getDisplayMetrics().density);
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
