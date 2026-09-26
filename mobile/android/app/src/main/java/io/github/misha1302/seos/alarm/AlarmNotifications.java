package io.github.misha1302.seos.alarm;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.media.AudioAttributes;
import android.media.RingtoneManager;
import android.os.Build;
import androidx.core.app.NotificationCompat;
import androidx.core.app.NotificationManagerCompat;
import io.github.misha1302.seos.MainActivity;
import io.github.misha1302.seos.R;
import java.text.DateFormat;
import java.util.Date;

/** Notifications of the alarm: the ringing one (full screen), the awake check, missed. */
public final class AlarmNotifications {
    /** Silent channel: the ringing sound is played by {@link AlarmService} on the alarm stream. */
    static final String RINGING = "wake_alarm";
    static final String CHECK = "wake_check";
    static final int RINGING_ID = 7101;

    private AlarmNotifications() {}

    static void ensureChannels(Context context) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return;
        NotificationManager manager = context.getSystemService(NotificationManager.class);
        if (manager.getNotificationChannel(RINGING) == null) {
            NotificationChannel ringing = new NotificationChannel(RINGING, context.getString(R.string.alarm_channel), NotificationManager.IMPORTANCE_HIGH);
            ringing.setDescription(context.getString(R.string.alarm_channel_description));
            ringing.setSound(null, null);
            ringing.setLockscreenVisibility(Notification.VISIBILITY_PUBLIC);
            manager.createNotificationChannel(ringing);
        }
        if (manager.getNotificationChannel(CHECK) == null) {
            NotificationChannel check = new NotificationChannel(CHECK, context.getString(R.string.alarm_check_channel), NotificationManager.IMPORTANCE_HIGH);
            check.setSound(RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM),
                    new AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_ALARM).build());
            manager.createNotificationChannel(check);
        }
    }

    static PendingIntent answer(Context context, String action, AlarmState state) {
        Intent intent = new Intent(context, AlarmReceiver.class).setAction(action).putExtra(AlarmReceiver.EXTRA_ID, state.id);
        return PendingIntent.getBroadcast(context, (state.id + action).hashCode(), intent,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
    }

    static Notification ringing(Context context, AlarmState state) {
        ensureChannels(context);
        Intent screen = new Intent(context, AlarmActivity.class).putExtra(AlarmReceiver.EXTRA_ID, state.id)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_NO_USER_ACTION);
        PendingIntent full = PendingIntent.getActivity(context, state.id.hashCode(), screen,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        String time = DateFormat.getTimeInstance(DateFormat.SHORT).format(new Date(state.at));
        return new NotificationCompat.Builder(context, RINGING)
                .setSmallIcon(R.drawable.ic_stat_reminder)
                .setContentTitle(state.title.isEmpty() ? time : state.title)
                .setContentText(time)
                .setCategory(NotificationCompat.CATEGORY_ALARM)
                .setPriority(NotificationCompat.PRIORITY_MAX)
                .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
                .setOngoing(true)
                .setAutoCancel(false)
                .setFullScreenIntent(full, true)
                .setContentIntent(full)
                .addAction(0, AlarmStore.label(context, state.wakeCheck ? "alarm_up" : "alarm_done"),
                        answer(context, AlarmReceiver.ACTION_UP, state))
                .addAction(0, AlarmStore.label(context, "alarm_snooze"), answer(context, AlarmReceiver.ACTION_SNOOZE, state))
                .build();
    }

    static void showAwakeCheck(Context context, AlarmState state) {
        ensureChannels(context);
        Intent screen = new Intent(context, MainActivity.class).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        NotificationCompat.Builder builder = new NotificationCompat.Builder(context, CHECK)
                .setSmallIcon(R.drawable.ic_stat_reminder)
                .setContentTitle(AlarmStore.label(context, "awake_title"))
                .setContentText(AlarmStore.label(context, "awake_body"))
                .setStyle(new NotificationCompat.BigTextStyle().bigText(AlarmStore.label(context, "awake_body")))
                .setCategory(NotificationCompat.CATEGORY_ALARM)
                .setPriority(NotificationCompat.PRIORITY_MAX)
                .setOngoing(true)
                .setContentIntent(answer(context, AlarmReceiver.ACTION_AWAKE, state))
                .addAction(0, AlarmStore.label(context, "awake_ok"), answer(context, AlarmReceiver.ACTION_AWAKE, state));
        notify(context, "seos-awake:" + state.id, builder.build());
    }

    static void showAwakeWait(Context context, AlarmState state) {
        ensureChannels(context);
        NotificationCompat.Builder builder = new NotificationCompat.Builder(context, CHECK)
                .setSmallIcon(R.drawable.ic_stat_reminder)
                .setContentTitle(AlarmStore.label(context, "awake_wait"))
                .setSilent(true)
                .setTimeoutAfter(10_000)
                .setPriority(NotificationCompat.PRIORITY_LOW);
        notify(context, "seos-awake:" + state.id, builder.build());
    }

    static void cancelAwakeCheck(Context context, AlarmState state) {
        NotificationManagerCompat.from(context).cancel("seos-awake:" + state.id, RINGING_ID);
    }

    static void showMissed(Context context, AlarmState state) {
        ensureChannels(context);
        Intent open = new Intent(context, MainActivity.class).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        NotificationCompat.Builder builder = new NotificationCompat.Builder(context, CHECK)
                .setSmallIcon(R.drawable.ic_stat_reminder)
                .setContentTitle(AlarmStore.label(context, "alarm_missed").replace("{title}", state.title))
                .setSilent(true)
                .setAutoCancel(true)
                .setContentIntent(PendingIntent.getActivity(context, 7301, open, PendingIntent.FLAG_IMMUTABLE));
        notify(context, "seos-missed:" + state.id, builder.build());
    }

    private static void notify(Context context, String tag, Notification notification) {
        try {
            NotificationManagerCompat.from(context).notify(tag, RINGING_ID, notification);
        } catch (SecurityException denied) {
            // Notifications are off; the alarm sound and screen still work.
        }
    }
}
