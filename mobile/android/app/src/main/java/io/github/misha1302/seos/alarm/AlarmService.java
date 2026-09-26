package io.github.misha1302.seos.alarm;

import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.media.AudioAttributes;
import android.media.AudioManager;
import android.media.MediaPlayer;
import android.media.RingtoneManager;
import android.net.Uri;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.PowerManager;
import android.os.VibrationEffect;
import android.os.Vibrator;
import android.util.Log;
import androidx.core.app.ServiceCompat;
import androidx.core.content.ContextCompat;

/**
 * Rings one alarm: a foreground service that plays the alarm sound in a loop on the
 * alarm stream, vibrates and shows the full-screen alarm. When the alarm asked for it
 * the alarm volume is raised to the maximum and restored afterwards — also after a
 * crash or reboot (the original value is persisted first). After {@link
 * AlarmState#RING_LIMIT} without an answer it stops and the alarm rings again later.
 */
public class AlarmService extends Service {
    private static final String TAG = "SeosAlarm";
    static final String ACTION_RING = "ring";
    static final String ACTION_STOP = "stop";

    private MediaPlayer player;
    private Vibrator vibrator;
    private PowerManager.WakeLock wakeLock;
    private String ringing;
    private final Handler handler = new Handler(Looper.getMainLooper());

    static void start(Context context, String id) {
        Intent intent = new Intent(context, AlarmService.class).setAction(ACTION_RING).putExtra(AlarmReceiver.EXTRA_ID, id);
        ContextCompat.startForegroundService(context, intent);
    }

    static void stop(Context context, String id) {
        Intent intent = new Intent(context, AlarmService.class).setAction(ACTION_STOP).putExtra(AlarmReceiver.EXTRA_ID, id);
        try {
            context.startService(intent);
        } catch (IllegalStateException notRunning) {
            restoreVolume(context);  // the service is gone; still put the volume back
        }
        context.sendBroadcast(new Intent(AlarmActivity.ACTION_CLOSED).setPackage(context.getPackageName()));
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        String id = intent == null ? null : intent.getStringExtra(AlarmReceiver.EXTRA_ID);
        if (intent == null || ACTION_STOP.equals(intent.getAction())) {
            if (id == null || id.equals(ringing)) finish();
            return START_NOT_STICKY;
        }
        AlarmState state = id == null ? null : AlarmStore.get(this, id);
        if (state == null || !AlarmState.RINGING.equals(state.phase)) {
            finish();
            return START_NOT_STICKY;
        }
        goForeground(state);
        if (ringing != null && !ringing.equals(id)) {
            // Another alarm was ringing: it rests and comes back like an unanswered one.
            AlarmReceiver.handle(this, AlarmReceiver.ACTION_TIMEOUT, ringing, null);
        }
        ringing = id;
        startRinging(state);
        handler.removeCallbacksAndMessages(null);
        final String current = id;
        handler.postDelayed(() -> AlarmReceiver.handle(this, AlarmReceiver.ACTION_TIMEOUT, current, null), AlarmState.RING_LIMIT);
        try {
            startActivity(new Intent(this, AlarmActivity.class).putExtra(AlarmReceiver.EXTRA_ID, id)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_NO_USER_ACTION));
        } catch (RuntimeException blocked) {
            // Background activity starts may be blocked; the full-screen notification shows it.
        }
        return START_NOT_STICKY;
    }

    private void goForeground(AlarmState state) {
        int type = 0;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            // Alarm apps allowed to schedule exact alarms may use systemExempted; otherwise
            // the alarm is media playback.
            type = AlarmScheduler.exactAllowed(this) ? ServiceInfo.FOREGROUND_SERVICE_TYPE_SYSTEM_EXEMPTED
                    : ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK;
        } else if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            type = ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK;
        }
        ServiceCompat.startForeground(this, AlarmNotifications.RINGING_ID, AlarmNotifications.ringing(this, state), type);
    }

    private void startRinging(AlarmState state) {
        stopSound();
        AudioManager audio = getSystemService(AudioManager.class);
        if (state.raiseVolume && audio != null) {
            int current = audio.getStreamVolume(AudioManager.STREAM_ALARM);
            int max = audio.getStreamMaxVolume(AudioManager.STREAM_ALARM);
            AlarmStore.rememberVolume(this, current);
            try {
                audio.setStreamVolume(AudioManager.STREAM_ALARM, max, 0);
            } catch (SecurityException dnd) {
                Log.w(TAG, "alarm volume could not be raised");
            }
        }
        PowerManager power = getSystemService(PowerManager.class);
        if (power != null && wakeLock == null) {
            wakeLock = power.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "seos:alarm");
            wakeLock.acquire(AlarmState.RING_LIMIT + 60_000L);
        }
        Uri sound = RingtoneManager.getActualDefaultRingtoneUri(this, RingtoneManager.TYPE_ALARM);
        if (sound == null) sound = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_RINGTONE);
        try {
            player = new MediaPlayer();
            player.setAudioAttributes(new AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_ALARM)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build());
            player.setDataSource(this, sound);
            player.setLooping(true);
            player.prepare();
            player.start();
        } catch (Exception failed) {  // no ringtone file, audio focus refused …
            Log.w(TAG, "alarm sound failed; vibration only", failed);
        }
        vibrator = getSystemService(Vibrator.class);
        if (vibrator != null && vibrator.hasVibrator()) {
            long[] pattern = {0, 800, 600};
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) vibrator.vibrate(VibrationEffect.createWaveform(pattern, 0));
            else vibrator.vibrate(pattern, 0);
        }
    }

    private void stopSound() {
        if (player != null) {
            try {
                player.stop();
            } catch (IllegalStateException ignored) {
                // already stopped
            }
            player.release();
            player = null;
        }
        if (vibrator != null) vibrator.cancel();
    }

    static void restoreVolume(Context context) {
        int volume = AlarmStore.takeVolume(context);
        AudioManager audio = context.getSystemService(AudioManager.class);
        if (volume >= 0 && audio != null) {
            try {
                audio.setStreamVolume(AudioManager.STREAM_ALARM, volume, 0);
            } catch (SecurityException ignored) {
                // Do Not Disturb policy: leave it
            }
        }
    }

    private void finish() {
        handler.removeCallbacksAndMessages(null);
        stopSound();
        restoreVolume(this);
        if (wakeLock != null && wakeLock.isHeld()) wakeLock.release();
        wakeLock = null;
        ringing = null;
        ServiceCompat.stopForeground(this, ServiceCompat.STOP_FOREGROUND_REMOVE);
        stopSelf();
    }

    @Override
    public void onDestroy() {
        handler.removeCallbacksAndMessages(null);
        stopSound();
        restoreVolume(this);
        if (wakeLock != null && wakeLock.isHeld()) wakeLock.release();
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}
