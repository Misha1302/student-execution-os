package io.github.misha1302.seos.reminders;

import android.util.Log;
import androidx.annotation.NonNull;
import com.capacitorjs.plugins.pushnotifications.MessagingService;
import com.google.firebase.messaging.RemoteMessage;
import java.util.Map;
import org.json.JSONException;

/**
 * Receives FCM messages for the app (replaces the Capacitor plugin's service in the
 * manifest and extends it, so token handling and JS events keep working).
 *
 * <p>Reminders marked {@code render=native} arrive as data-only messages; they are
 * delivered here even when the app is in the background or killed, and are rendered
 * with working action buttons. The web layer is still told so an open app refreshes.
 */
public class ReminderMessagingService extends MessagingService {
    @Override
    public void onMessageReceived(@NonNull RemoteMessage message) {
        Map<String, String> data = message.getData();
        if ("reminder".equals(data.get("type")) && "native".equals(data.get("render"))) {
            try {
                ReminderNotifications.show(this, ReminderNotifications.parse(data));
            } catch (JSONException malformed) {
                Log.w("SeosReminders", "reminder push with malformed data was not shown");
            }
        }
        super.onMessageReceived(message);
    }
}
