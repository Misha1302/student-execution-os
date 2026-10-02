package io.github.misha1302.seos.storage;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

/**
 * Pure rules for the offline operation queue (no Android types, unit-tested on the JVM).
 *
 * <p>A queued item is the web client's JSON object {@code {operation:{op_id,...}, state, ...}}.
 * The op_id is the server's exactly-once key: it is never rewritten, and one scope never
 * holds two items with the same op_id.
 */
public final class QueueItems {
    private QueueItems() {}

    /** op_id → item JSON, in queue order; rejects malformed items and duplicate op_ids. */
    public static LinkedHashMap<String, String> parse(String itemsJson) throws JSONException {
        JSONArray array = new JSONArray(itemsJson);
        LinkedHashMap<String, String> items = new LinkedHashMap<>();
        for (int index = 0; index < array.length(); index++) {
            JSONObject item = array.getJSONObject(index);
            JSONObject operation = item.optJSONObject("operation");
            String opId = operation == null ? "" : operation.optString("op_id", "");
            if (opId.isEmpty()) throw new JSONException("queued item " + index + " has no operation.op_id");
            if (items.containsKey(opId)) throw new JSONException("duplicate op_id in one queue: " + opId);
            items.put(opId, item.toString());
        }
        return items;
    }

    /** Legacy items whose op_id the native queue does not hold yet, in their legacy order. */
    public static List<Map.Entry<String, String>> missing(Map<String, String> legacy, Set<String> present) {
        List<Map.Entry<String, String>> result = new ArrayList<>();
        for (Map.Entry<String, String> entry : legacy.entrySet()) {
            if (!present.contains(entry.getKey())) result.add(entry);
        }
        return result;
    }

    /** Whether every legacy op_id is now in the native queue (the migration is verified). */
    public static boolean covers(Set<String> present, Map<String, String> legacy) {
        return present.containsAll(legacy.keySet());
    }
}
