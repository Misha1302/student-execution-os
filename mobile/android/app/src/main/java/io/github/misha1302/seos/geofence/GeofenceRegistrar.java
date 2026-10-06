package io.github.misha1302.seos.geofence;

import android.Manifest;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.location.LocationManager;
import android.os.Build;
import androidx.core.content.ContextCompat;
import java.util.List;
import org.json.JSONException;
import org.json.JSONObject;

/**
 * Registers the watched triggers with the platform's own proximity alerts
 * ({@link LocationManager#addProximityAlert}): no Google Play services are needed, so
 * it also works on phones without them. Registration needs the fine-location
 * permission; without "allow all the time" (Android 10+) alerts arrive only while the
 * app is in use, which {@link #status} reports so the app can say so honestly.
 */
public final class GeofenceRegistrar {
    private GeofenceRegistrar() {}

    static boolean fineAllowed(Context context) {
        return ContextCompat.checkSelfPermission(context, Manifest.permission.ACCESS_FINE_LOCATION)
                == PackageManager.PERMISSION_GRANTED;
    }

    static boolean backgroundAllowed(Context context) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) return fineAllowed(context);
        return ContextCompat.checkSelfPermission(context, Manifest.permission.ACCESS_BACKGROUND_LOCATION)
                == PackageManager.PERMISSION_GRANTED;
    }

    static PendingIntent intent(Context context, String triggerId) {
        Intent intent = new Intent(context, GeofenceReceiver.class)
                .setAction("io.github.misha1302.seos.GEOFENCE")
                .putExtra(GeofenceReceiver.EXTRA_TRIGGER, triggerId);
        // Mutable: the platform adds KEY_PROXIMITY_ENTERING to it on every crossing.
        int flags = PendingIntent.FLAG_UPDATE_CURRENT | (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S ? PendingIntent.FLAG_MUTABLE : 0);
        return PendingIntent.getBroadcast(context, GeofenceDecisions.requestCode(triggerId), intent, flags);
    }

    /** Replaces the registered alerts with {@code specs}; returns how many are registered. */
    public static int apply(Context context, List<GeofenceSpec> specs) {
        LocationManager manager = context.getSystemService(LocationManager.class);
        for (GeofenceSpec old : GeofenceStore.all(context)) {
            boolean kept = false;
            for (GeofenceSpec spec : specs) if (spec.sameArea(old)) kept = true;
            if (!kept && manager != null) manager.removeProximityAlert(intent(context, old.id));
        }
        GeofenceStore.save(context, specs);
        return registerAll(context);
    }

    /** (Re-)registers every stored trigger, e.g. after a reboot or an app update. */
    public static int registerAll(Context context) {
        LocationManager manager = context.getSystemService(LocationManager.class);
        if (manager == null || !fineAllowed(context)) return 0;
        int registered = 0;
        for (GeofenceSpec spec : GeofenceStore.all(context)) {
            try {
                manager.addProximityAlert(spec.latitude, spec.longitude, spec.radiusMeters, -1, intent(context, spec.id));
                registered++;
            } catch (SecurityException | IllegalArgumentException revoked) {
                // Permission withdrawn meanwhile, or no location provider: reported by status().
            }
        }
        return registered;
    }

    public static void clear(Context context) {
        LocationManager manager = context.getSystemService(LocationManager.class);
        for (GeofenceSpec spec : GeofenceStore.all(context)) {
            if (manager != null) manager.removeProximityAlert(intent(context, spec.id));
        }
        GeofenceStore.clear(context);
    }

    public static void forget(Context context, String triggerId) {
        LocationManager manager = context.getSystemService(LocationManager.class);
        if (manager != null) manager.removeProximityAlert(intent(context, triggerId));
        GeofenceStore.remove(context, triggerId);
    }

    public static JSONObject status(Context context) throws JSONException {
        LocationManager manager = context.getSystemService(LocationManager.class);
        boolean enabled = manager != null && (Build.VERSION.SDK_INT < Build.VERSION_CODES.P || manager.isLocationEnabled());
        return GeofenceStore.summary(context).put("fine", fineAllowed(context)).put("background", backgroundAllowed(context))
                .put("location_enabled", enabled);
    }
}
