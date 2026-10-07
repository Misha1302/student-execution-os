package io.github.misha1302.seos.geofence;

import android.content.Context;
import android.content.SharedPreferences;
import java.util.ArrayList;
import java.util.List;
import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

/**
 * The armed triggers this phone watches and when each last fired, kept across process
 * death and reboots (proximity alerts do not survive a reboot; {@link GeofenceRegistrar}
 * puts them back). Bound to the signed-in account: another account's triggers are
 * cleared before anything of the new account is stored.
 */
final class GeofenceStore {
    private static final String PREFS = "seos.geofences";

    private GeofenceStore() {}

    private static SharedPreferences prefs(Context context) {
        return context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    static synchronized List<GeofenceSpec> all(Context context) {
        try {
            return GeofenceSpec.listFromJson(new JSONArray(prefs(context).getString("specs", "[]")));
        } catch (JSONException unreadable) {
            return new ArrayList<>();
        }
    }

    static synchronized GeofenceSpec find(Context context, String id) {
        for (GeofenceSpec spec : all(context)) if (spec.id.equals(id)) return spec;
        return null;
    }

    static synchronized void save(Context context, List<GeofenceSpec> specs) {
        JSONArray out = new JSONArray();
        try {
            for (GeofenceSpec spec : specs) out.put(spec.toJson());
        } catch (JSONException impossible) {
            throw new IllegalStateException(impossible);
        }
        prefs(context).edit().putString("specs", out.toString()).apply();
    }

    static synchronized void remove(Context context, String id) {
        List<GeofenceSpec> kept = new ArrayList<>();
        for (GeofenceSpec spec : all(context)) if (!spec.id.equals(id)) kept.add(spec);
        save(context, kept);
    }

    static synchronized long lastFired(Context context, String id) {
        return prefs(context).getLong("fired." + id, 0L);
    }

    static synchronized void setLastFired(Context context, String id, long at) {
        // Synchronous: the one-shot guard must survive the receiver's process being killed.
        prefs(context).edit().putLong("fired." + id, at).commit();
    }

    static synchronized String owner(Context context) {
        return prefs(context).getString("owner", null);
    }

    static synchronized void setOwner(Context context, String owner) {
        prefs(context).edit().putString("owner", owner).apply();
    }

    static synchronized void clear(Context context) {
        prefs(context).edit().clear().apply();
    }

    static JSONObject summary(Context context) throws JSONException {
        return new JSONObject().put("watched", all(context).size());
    }
}
