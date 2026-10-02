package io.github.misha1302.seos.storage;

import android.content.Context;
import android.content.SharedPreferences;

/**
 * The one owner of the session token on the phone.
 *
 * <p>The token lives in {@link KeystoreVault} (AES-GCM, key generated inside the Android
 * Keystore and never exportable). Older app versions kept it in plain Capacitor
 * Preferences ({@code CapacitorStorage / seos.token}); {@link #migrate()} moves it:
 *
 * <pre>
 *   read legacy → write secure → read secure back and compare → mark migrated → remove legacy
 * </pre>
 *
 * <p>Any failure leaves the legacy value where it is and reports the failure — the
 * credential is never lost silently and a failed migration is never reported as done.
 * A legacy value that appears after migration can only have been written by an older
 * app version (a rollback build); it is the newest login, so it wins and is migrated.
 * The server URL and user are not secrets and stay in Preferences.
 */
public final class SessionCredentials {
    public static final String LEGACY_PREFS = "CapacitorStorage";
    public static final String LEGACY_TOKEN = "seos.token";
    static final String STATE_PREFS = "seos_credential_state";
    static final String MIGRATED = "token_migrated_v1";

    /** Migration outcome codes reported to the web client (never the value). */
    public enum Migration { NOTHING_TO_MIGRATE, MIGRATED, VERIFY_FAILED, SECURE_STORE_UNAVAILABLE }

    /** The plain key/value storage the legacy token and the migration marker live in. */
    public interface Legacy {
        String token();

        boolean removeToken();

        boolean migrated();

        void markMigrated();

        /** Whether a signed-in user is recorded (written before the token, removed on sign-out). */
        boolean hasUser();
    }

    private final CredentialVault vault;
    private final Legacy legacy;

    public SessionCredentials(CredentialVault vault, Legacy legacy) {
        this.vault = vault;
        this.legacy = legacy;
    }

    public static SessionCredentials of(Context context) {
        Context app = context.getApplicationContext();
        return new SessionCredentials(new KeystoreVault(app), new PreferencesLegacy(app));
    }

    public synchronized Migration migrate() {
        String old = legacy.token();
        if (old == null || old.isEmpty()) return Migration.NOTHING_TO_MIGRATE;
        try {
            vault.write(old);
            if (!old.equals(vault.read())) return Migration.VERIFY_FAILED;
        } catch (Exception keystore) {
            return Migration.SECURE_STORE_UNAVAILABLE;
        }
        legacy.markMigrated();
        return legacy.removeToken() ? Migration.MIGRATED : Migration.VERIFY_FAILED;
    }

    /** The current token: migrates first, so a legacy-only token is still returned. */
    public synchronized String token() {
        Migration outcome = migrate();
        if (outcome == Migration.VERIFY_FAILED || outcome == Migration.SECURE_STORE_UNAVAILABLE) {
            return legacy.token();  // not migrated: the legacy copy is still the credential
        }
        if (!legacy.hasUser()) {
            // Signed out — possibly by an older app version that only knows the legacy
            // keys: the secure copy must not sign the user back in.
            vault.clear();
            return null;
        }
        try {
            return vault.read();
        } catch (Exception unreadable) {
            return null;  // e.g. the Keystore key was wiped: the user signs in again
        }
    }

    /** Stores a new token (null = sign out). Never falls back to plain storage. */
    public synchronized void setToken(String value) throws Exception {
        if (value == null || value.isEmpty()) {
            clear();
            return;
        }
        vault.write(value);
        if (!value.equals(vault.read())) throw new IllegalStateException("secure credential verification failed");
        legacy.removeToken();  // a stale legacy copy must not outlive a new login
    }

    /** Sign-out / server switch: both the secure and the legacy location are cleared. */
    public synchronized void clear() {
        vault.clear();
        legacy.removeToken();
    }

    public synchronized boolean migratedBefore() {
        return legacy.migrated();
    }

    static final class PreferencesLegacy implements Legacy {
        private final SharedPreferences prefs;
        private final SharedPreferences state;

        PreferencesLegacy(Context context) {
            prefs = context.getSharedPreferences(LEGACY_PREFS, Context.MODE_PRIVATE);
            state = context.getSharedPreferences(STATE_PREFS, Context.MODE_PRIVATE);
        }

        @Override public String token() { return prefs.getString(LEGACY_TOKEN, null); }

        @Override public boolean removeToken() { return prefs.edit().remove(LEGACY_TOKEN).commit(); }

        @Override public boolean migrated() { return state.getBoolean(MIGRATED, false); }

        @Override public void markMigrated() { state.edit().putBoolean(MIGRATED, true).commit(); }

        @Override public boolean hasUser() {
            String user = prefs.getString("seos.user", null);
            return user != null && !user.isEmpty() && !"null".equals(user);
        }
    }
}
