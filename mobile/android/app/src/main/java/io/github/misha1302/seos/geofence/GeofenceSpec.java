package io.github.misha1302.seos.geofence;

import java.util.ArrayList;
import java.util.List;
import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

/** One armed location trigger as the phone watches it (from GET /api/v1/location-triggers/armed). */
public final class GeofenceSpec {
    public final String id;
    public final String title;
    public final String transition;  // ENTER | EXIT
    public final double latitude;
    public final double longitude;
    public final float radiusMeters;
    public final boolean repeat;
    public final String placeName;

    public GeofenceSpec(String id, String title, String transition, double latitude, double longitude,
                        float radiusMeters, boolean repeat, String placeName) {
        if (id == null || id.isEmpty()) throw new IllegalArgumentException("trigger id is required");
        if (!"ENTER".equals(transition) && !"EXIT".equals(transition)) throw new IllegalArgumentException("bad transition");
        if (latitude < -90 || latitude > 90 || longitude < -180 || longitude > 180) throw new IllegalArgumentException("bad position");
        this.id = id;
        this.title = title == null ? "" : title;
        this.transition = transition;
        this.latitude = latitude;
        this.longitude = longitude;
        this.radiusMeters = Math.max(50f, Math.min(5000f, radiusMeters));
        this.repeat = repeat;
        this.placeName = placeName == null ? "" : placeName;
    }

    public static GeofenceSpec fromJson(JSONObject item) throws JSONException {
        return new GeofenceSpec(item.getString("id"), item.optString("title"), item.optString("transition", "ENTER"),
                item.getDouble("latitude"), item.getDouble("longitude"), (float) item.optDouble("radius_meters", 150),
                item.optBoolean("repeat", false), item.optString("place_name"));
    }

    public JSONObject toJson() throws JSONException {
        return new JSONObject().put("id", id).put("title", title).put("transition", transition).put("latitude", latitude)
                .put("longitude", longitude).put("radius_meters", radiusMeters).put("repeat", repeat).put("place_name", placeName);
    }

    public static List<GeofenceSpec> listFromJson(JSONArray items) throws JSONException {
        List<GeofenceSpec> out = new ArrayList<>();
        for (int i = 0; i < items.length(); i++) out.add(fromJson(items.getJSONObject(i)));
        return out;
    }

    /** Same geofence on the ground (a changed title alone does not need a re-registration). */
    public boolean sameArea(GeofenceSpec other) {
        return other != null && id.equals(other.id) && transition.equals(other.transition)
                && latitude == other.latitude && longitude == other.longitude && radiusMeters == other.radiusMeters;
    }
}
