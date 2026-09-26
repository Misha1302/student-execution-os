package io.github.misha1302.seos.reminders;

import java.util.Arrays;
import java.util.HashSet;
import java.util.Set;
import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

/** Semantic policy for HTTP-200 sync results returned to notification workers. */
public final class SyncResultPolicy {
    public enum Decision { SUCCESS, PERMANENT_FAILURE, RETRY }

    private static final Set<String> SAFE_NOOPS = new HashSet<>(Arrays.asList(
            "ALREADY_COMPLETED", "ALREADY_DONE", "ALREADY_CANCELLED", "ALREADY_STARTED",
            "ALREADY_OPEN", "ALREADY_UP", "ALREADY_EXISTS", "NOTHING_TO_CHANGE",
            "REMINDER_CLOSED", "SNOOZE_EXPIRED", "DELETED", "SUPERSEDED"));

    private SyncResultPolicy() {}

    public static Decision decide(JSONArray results) throws JSONException {
        if (results.length() == 0) return Decision.RETRY;
        for (int i = 0; i < results.length(); i++) {
            JSONObject result = results.getJSONObject(i);
            String status = result.optString("status");
            if ("APPLIED".equals(status)) continue;
            if ("NOOP".equals(status) && SAFE_NOOPS.contains(result.optString("code"))) continue;
            if ("CONFLICT".equals(status) || "REJECTED".equals(status) || "NOOP".equals(status)) {
                return Decision.PERMANENT_FAILURE;
            }
            return Decision.RETRY;
        }
        return Decision.SUCCESS;
    }
}
