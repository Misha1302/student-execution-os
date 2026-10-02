package io.github.misha1302.seos.storage;

import android.content.ContentValues;
import android.content.Context;
import android.database.Cursor;
import android.database.sqlite.SQLiteDatabase;
import android.database.sqlite.SQLiteOpenHelper;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

/**
 * The durable offline operation queue: one SQLite database in the app's private storage.
 *
 * <p>Rows are keyed by (scope, op_id); scope is "server|account", so two accounts or two
 * servers never see each other's operations. Every write is one transaction that is
 * committed (and synced by SQLite) before the call returns, so an operation the user was
 * told is saved survives the app being killed. op_ids are stored exactly as queued.
 */
public final class OfflineQueueDb extends SQLiteOpenHelper {
    static final String NAME = "seos_offline.db";
    private static final int VERSION = 1;
    private static OfflineQueueDb instance;

    public static synchronized OfflineQueueDb get(Context context) {
        if (instance == null) instance = new OfflineQueueDb(context.getApplicationContext(), NAME);
        return instance;
    }

    OfflineQueueDb(Context context, String name) {
        super(context, name, null, VERSION);
        setWriteAheadLoggingEnabled(true);
    }

    @Override
    public void onCreate(SQLiteDatabase db) {
        db.execSQL("CREATE TABLE queue (scope TEXT NOT NULL, position INTEGER NOT NULL, op_id TEXT NOT NULL, "
                + "item_json TEXT NOT NULL, PRIMARY KEY (scope, op_id))");
        db.execSQL("CREATE INDEX queue_order ON queue(scope, position)");
        db.execSQL("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)");
    }

    @Override
    public void onUpgrade(SQLiteDatabase db, int oldVersion, int newVersion) {
        // Version 1 is the first schema; future versions migrate here, never drop.
    }

    /** Replaces one scope's queue atomically (the web client's write of the whole list). */
    public synchronized void replace(String scope, String itemsJson) throws JSONException {
        LinkedHashMap<String, String> items = QueueItems.parse(itemsJson);  // validates before touching the DB
        SQLiteDatabase db = getWritableDatabase();
        db.beginTransaction();
        try {
            db.delete("queue", "scope=?", new String[] {scope});
            int position = 0;
            for (Map.Entry<String, String> item : items.entrySet()) insert(db, scope, position++, item.getKey(), item.getValue());
            db.setTransactionSuccessful();
        } finally {
            db.endTransaction();
        }
    }

    /**
     * Imports a legacy (WebView storage) queue: only op_ids not already present are added,
     * after the existing items, so importing twice — or after a crash half-way — never
     * duplicates an operation. Returns how many were added.
     */
    public synchronized int importLegacy(String scope, String itemsJson) throws JSONException {
        LinkedHashMap<String, String> legacy = QueueItems.parse(itemsJson);
        SQLiteDatabase db = getWritableDatabase();
        db.beginTransaction();
        try {
            Set<String> present = opIds(db, scope);
            List<Map.Entry<String, String>> missing = QueueItems.missing(legacy, present);
            int position = nextPosition(db, scope);
            for (Map.Entry<String, String> item : missing) insert(db, scope, position++, item.getKey(), item.getValue());
            db.setTransactionSuccessful();
            return missing.size();
        } finally {
            db.endTransaction();
        }
    }

    /** Whether every op_id of a legacy queue is in the native queue (migration verification). */
    public synchronized boolean covers(String scope, String itemsJson) throws JSONException {
        return QueueItems.covers(opIds(getReadableDatabase(), scope), QueueItems.parse(itemsJson));
    }

    /** {scope: [items...]} for every scope, in queue order. */
    public synchronized JSONObject loadAll() throws JSONException {
        JSONObject out = new JSONObject();
        try (Cursor cursor = getReadableDatabase().query("queue", new String[] {"scope", "item_json"}, null, null,
                null, null, "scope, position")) {
            while (cursor.moveToNext()) {
                String scope = cursor.getString(0);
                JSONArray items = out.optJSONArray(scope);
                if (items == null) {
                    items = new JSONArray();
                    out.put(scope, items);
                }
                items.put(new JSONObject(cursor.getString(1)));
            }
        }
        return out;
    }

    public synchronized String meta(String key) {
        try (Cursor cursor = getReadableDatabase().query("meta", new String[] {"value"}, "key=?", new String[] {key},
                null, null, null)) {
            return cursor.moveToFirst() ? cursor.getString(0) : null;
        }
    }

    public synchronized void setMeta(String key, String value) {
        ContentValues values = new ContentValues();
        values.put("key", key);
        values.put("value", value);
        getWritableDatabase().insertWithOnConflict("meta", null, values, SQLiteDatabase.CONFLICT_REPLACE);
    }

    private static void insert(SQLiteDatabase db, String scope, int position, String opId, String json) {
        ContentValues values = new ContentValues();
        values.put("scope", scope);
        values.put("position", position);
        values.put("op_id", opId);
        values.put("item_json", json);
        db.insertOrThrow("queue", null, values);
    }

    private static Set<String> opIds(SQLiteDatabase db, String scope) {
        Set<String> ids = new HashSet<>();
        try (Cursor cursor = db.query("queue", new String[] {"op_id"}, "scope=?", new String[] {scope}, null, null, null)) {
            while (cursor.moveToNext()) ids.add(cursor.getString(0));
        }
        return ids;
    }

    private static int nextPosition(SQLiteDatabase db, String scope) {
        try (Cursor cursor = db.rawQuery("SELECT COALESCE(MAX(position), -1) + 1 FROM queue WHERE scope=?", new String[] {scope})) {
            return cursor.moveToFirst() ? cursor.getInt(0) : 0;
        }
    }
}
