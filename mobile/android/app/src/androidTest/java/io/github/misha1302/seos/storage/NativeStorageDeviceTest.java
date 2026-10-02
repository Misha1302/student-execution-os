package io.github.misha1302.seos.storage;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertThrows;
import static org.junit.Assert.assertTrue;

import android.content.Context;
import androidx.test.core.app.ApplicationProvider;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;
import org.junit.After;
import org.junit.Before;
import org.junit.Test;
import org.junit.runner.RunWith;

/** The native queue and credential store on a real Android system (SQLite, Android Keystore). */
@RunWith(AndroidJUnit4.class)
public class NativeStorageDeviceTest {
    private static final String DB = "seos_offline_test.db";
    private Context context;

    private static String item(String opId) {
        return "{\"operation\":{\"op_id\":\"" + opId + "\",\"type\":\"task.start\",\"entity_id\":\"t1\",\"payload\":{}},"
                + "\"state\":\"PENDING\"}";
    }

    @Before
    public void setUp() {
        context = ApplicationProvider.getApplicationContext();
        context.deleteDatabase(DB);
        context.getSharedPreferences(SessionCredentials.LEGACY_PREFS, Context.MODE_PRIVATE).edit().clear().commit();
        context.getSharedPreferences(SessionCredentials.STATE_PREFS, Context.MODE_PRIVATE).edit().clear().commit();
        context.getSharedPreferences("seos_secure_test", Context.MODE_PRIVATE).edit().clear().commit();
    }

    @After
    public void tearDown() {
        context.deleteDatabase(DB);
    }

    @Test
    public void queuedOperationsSurviveAProcessRestart() throws JSONException {
        OfflineQueueDb first = new OfflineQueueDb(context, DB);
        first.replace("https://a|acct-1", "[" + item("op-1") + "," + item("op-2") + "]");
        first.close();  // the process dies; a new one opens the same file
        OfflineQueueDb second = new OfflineQueueDb(context, DB);
        JSONArray items = second.loadAll().getJSONArray("https://a|acct-1");
        assertEquals(2, items.length());
        assertEquals("op-1", items.getJSONObject(0).getJSONObject("operation").getString("op_id"));
        assertEquals("op-2", items.getJSONObject(1).getJSONObject("operation").getString("op_id"));
        second.close();
    }

    @Test
    public void legacyImportIsIdempotentAndVerified() throws JSONException {
        OfflineQueueDb db = new OfflineQueueDb(context, DB);
        db.replace("s", "[" + item("op-1") + "]");
        String legacy = "[" + item("op-1") + "," + item("op-2") + "]";
        assertFalse(db.covers("s", legacy));
        assertEquals(1, db.importLegacy("s", legacy));
        assertEquals(0, db.importLegacy("s", legacy));  // interrupted and re-run: nothing duplicated
        assertTrue(db.covers("s", legacy));
        assertEquals(2, db.loadAll().getJSONArray("s").length());
        db.close();
    }

    @Test
    public void scopesAreIsolatedAndABadWriteChangesNothing() throws JSONException {
        OfflineQueueDb db = new OfflineQueueDb(context, DB);
        db.replace("https://a|acct-1", "[" + item("op-1") + "]");
        db.replace("https://b|acct-2", "[" + item("op-9") + "]");
        assertThrows(JSONException.class, () -> db.replace("https://a|acct-1", "[" + item("op-1") + "," + item("op-1") + "]"));
        JSONObject all = db.loadAll();
        assertEquals(1, all.getJSONArray("https://a|acct-1").length());
        assertEquals("op-9", all.getJSONArray("https://b|acct-2").getJSONObject(0).getJSONObject("operation").getString("op_id"));
        db.close();
    }

    @Test
    public void theKeystoreVaultStoresOnlyCiphertext() throws Exception {
        KeystoreVault vault = new KeystoreVault(context, "seos_secure_test", "seos.session.credential.test");
        vault.write("token-value-123");
        assertEquals("token-value-123", vault.read());
        assertTrue(vault.storedValueDiffersFrom("token-value-123"));
        vault.clear();
        assertNull(vault.read());
    }

    @Test
    public void aLegacyPlainTokenIsMigratedIntoTheKeystore() {
        context.getSharedPreferences(SessionCredentials.LEGACY_PREFS, Context.MODE_PRIVATE).edit()
                .putString("seos.token", "legacy-token").putString("seos.user", "{\"account_id\":\"acct-1\"}").commit();
        SessionCredentials credentials = SessionCredentials.of(context);
        assertEquals(SessionCredentials.Migration.MIGRATED, credentials.migrate());
        assertNull(context.getSharedPreferences(SessionCredentials.LEGACY_PREFS, Context.MODE_PRIVATE).getString("seos.token", null));
        assertEquals("legacy-token", credentials.token());
        credentials.clear();
        assertNull(credentials.token());
    }
}
