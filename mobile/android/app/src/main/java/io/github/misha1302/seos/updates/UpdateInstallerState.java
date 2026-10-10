package io.github.misha1302.seos.updates;

import android.content.Context;
import android.content.SharedPreferences;

/** Durable, session-correlated owner of the PackageInstaller lifecycle. */
final class UpdateInstallerState {
    static final String PREFS = "seos-update-installer";
    // The plugin worker and the main-thread PackageInstaller receiver share one
    // process; every read-modify-write transition is serialized on this lock.
    private static final Object LOCK = new Object();

    interface Backend {
        Snapshot load();
        boolean save(Snapshot value);
    }

    static final class Snapshot {
        final int sessionId;
        final String state;
        final String code;
        final String message;
        final String targetVersion;
        final long targetBuild;
        final String targetSha256;
        final long updatedAt;

        Snapshot(int sessionId, String state, String code, String message, String targetVersion,
                 long targetBuild, String targetSha256, long updatedAt) {
            this.sessionId = sessionId;
            this.state = state;
            this.code = code;
            this.message = message;
            this.targetVersion = targetVersion;
            this.targetBuild = targetBuild;
            this.targetSha256 = targetSha256;
            this.updatedAt = updatedAt;
        }

        Snapshot with(String next, String nextCode, String nextMessage) {
            return new Snapshot(sessionId, next, nextCode, nextMessage, targetVersion,
                    targetBuild, targetSha256, System.currentTimeMillis());
        }
    }

    private static final class PreferencesBackend implements Backend {
        private final SharedPreferences preferences;
        PreferencesBackend(Context context) {
            preferences = context.getApplicationContext().getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        }
        @Override public Snapshot load() {
            return new Snapshot(
                    preferences.getInt("session_id", -1), preferences.getString("state", "IDLE"),
                    preferences.getString("code", ""), preferences.getString("message", ""),
                    preferences.getString("target_version", ""), preferences.getLong("target_build", 0),
                    preferences.getString("target_sha256", ""), preferences.getLong("updated_at", 0));
        }
        @Override public boolean save(Snapshot value) {
            // commit(), not apply(): losing this write can create a duplicate install
            // after process death.
            return preferences.edit().putInt("session_id", value.sessionId)
                    .putString("state", value.state).putString("code", value.code)
                    .putString("message", value.message).putString("target_version", value.targetVersion)
                    .putLong("target_build", value.targetBuild).putString("target_sha256", value.targetSha256)
                    .putLong("updated_at", value.updatedAt).commit();
        }
    }

    private final Backend backend;
    UpdateInstallerState(Context context) { this(new PreferencesBackend(context)); }
    UpdateInstallerState(Backend backend) { this.backend = backend; }

    Snapshot get() { synchronized (LOCK) { return backend.load(); } }

    boolean begin(int sessionId, String version, long build, String sha256) {
        synchronized (LOCK) {
            return backend.save(new Snapshot(sessionId, "PREPARING", "", "", version, build,
                    sha256, System.currentTimeMillis()));
        }
    }

    boolean markSubmitting(int sessionId) { return transition(sessionId, "SUBMITTING", "", "", "PREPARING"); }
    boolean markCommitted(int sessionId) { return transition(sessionId, "COMMITTED", "", "", "SUBMITTING"); }

    boolean reconcileInstalledBuild(long runningBuild) {
        synchronized (LOCK) {
            Snapshot current = backend.load();
            if (current.targetBuild <= 0 || runningBuild < current.targetBuild
                    || !("PREPARING".equals(current.state) || "SUBMITTING".equals(current.state)
                    || "COMMITTED".equals(current.state) || "USER_ACTION_REQUIRED".equals(current.state))) return false;
            // A manual same-signer upgrade can finish a target whose original
            // installer never reached commit. This is installed-package evidence,
            // not a PackageInstaller callback, so PREPARING is eligible too.
            return backend.save(current.with("INSTALLED", "", "Installed build is running"));
        }
    }

    boolean fail(int sessionId, String code, String message) {
        synchronized (LOCK) {
            Snapshot current = backend.load();
            if (current.sessionId != sessionId || terminal(current.state)) return false;
            return backend.save(current.with("FAILED", code, message == null ? "" : message));
        }
    }

    boolean callback(int sessionId, String next, String code, String message) {
        synchronized (LOCK) {
            Snapshot current = backend.load();
            if (current.sessionId != sessionId || terminal(current.state)) return false;
            if (!("COMMITTED".equals(current.state) || "USER_ACTION_REQUIRED".equals(current.state)
                    || "SUBMITTING".equals(current.state))) return false;
            return backend.save(current.with(next, code == null ? "" : code, message == null ? "" : message));
        }
    }

    private boolean transition(int sessionId, String next, String code, String message, String required) {
        synchronized (LOCK) {
            Snapshot current = backend.load();
            if (current.sessionId != sessionId || !required.equals(current.state)) return false;
            return backend.save(current.with(next, code, message));
        }
    }

    private static boolean terminal(String state) {
        return "INSTALLED".equals(state) || "FAILED".equals(state);
    }
}
