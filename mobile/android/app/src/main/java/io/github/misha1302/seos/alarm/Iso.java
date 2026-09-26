package io.github.misha1302.seos.alarm;

import java.text.ParseException;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;
import java.util.TimeZone;
import org.json.JSONException;

/** ISO-8601 instants as the server and the web client write them (minSdk 24: no java.time). */
public final class Iso {
    private Iso() {}

    public static long parse(String value) throws JSONException {
        String clean = value.trim().replaceFirst("\\.\\d+", "").replaceFirst("Z$", "+00:00");
        SimpleDateFormat format = new SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ssXXX", Locale.ROOT);
        try {
            return format.parse(clean).getTime();
        } catch (ParseException malformed) {
            throw new JSONException("not an ISO-8601 instant: " + value);
        }
    }

    public static String format(long millis) {
        SimpleDateFormat format = new SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss'Z'", Locale.ROOT);
        format.setTimeZone(TimeZone.getTimeZone("UTC"));
        return format.format(new Date(millis));
    }
}
