package io.github.misha1302.seos.reminders;

import android.util.Log;
import androidx.annotation.NonNull;
import com.capacitorjs.plugins.pushnotifications.MessagingService;
import com.google.firebase.messaging.RemoteMessage;
import io.github.misha1302.seos.alarm.AlarmPush;
import io.github.misha1302.seos.alarm.AlarmSyncWorker;
import java.util.Map;
import org.json.JSONException;

/**
 * Receives FCM messages for the app (replaces the Capacitor plugin's service in the
 * manifest and extends it, so token handling and JS events keep working).
 *
 * <p>Reminders marked {@code render=native} arrive as data-only messages; they are
 * delivered here even when the app is in the background or killed, and are rendered
 * with working action buttons. An alarm reminder rings through the alarm package
 * (and, for PUSH_AND_ALARM, also shows the notification); an "alarm-sync" signal makes
 * the phone fetch its alarm schedule. The web layer is still told so an open app refreshes.
 */
public class ReminderMessagingService extends MessagingService {
    @Override
    public void onMessageReceived(@NonNull RemoteMessage message) {
        Map<String, String> data = message.getData();
        boolean ours = "alarm-sync".equals(data.get("type")) || "reminder".equals(data.get("type"));
        if (ours && !AlarmSyncWorker.belongsToSession(this, data.get("account_id"))) {
            return;  // for an account this phone is no longer signed in to
        }
        if ("alarm-sync".equals(data.get("type"))) {
            AlarmPush.sync(this);
            return;  // a silent signal, nothing for the web layer
        }
        if ("reminder".equals(data.get("type")) && "native".equals(data.get("render"))) {
            try {
                boolean rang = AlarmPush.handle(this, data);
                if (!rang || "PUSH_AND_ALARM".equals(data.get("delivery"))) {
                    ReminderNotifications.show(this, ReminderNotifications.parse(data));
                }
            } catch (JSONException malformed) {
                Log.w("SeosReminders", "reminder push with malformed data was not shown");
            }
        }
        super.onMessageReceived(message);
    }
}
