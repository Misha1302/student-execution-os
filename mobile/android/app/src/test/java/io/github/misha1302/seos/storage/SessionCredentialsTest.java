package io.github.misha1302.seos.storage;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertThrows;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

public class SessionCredentialsTest {
    static final class FakeVault implements CredentialVault {
        String value;
        boolean failWrites;
        boolean corruptReads;
        int writes;

        @Override public String read() throws Exception {
            if (corruptReads && value != null) return value + "-corrupt";
            return value;
        }

        @Override public void write(String next) throws Exception {
            if (failWrites) throw new IllegalStateException("keystore unavailable");
            writes++;
            value = next;
        }

        @Override public void clear() { value = null; }
    }

    static final class FakeLegacy implements SessionCredentials.Legacy {
        String token;
        boolean user = true;
        boolean migrated;
        boolean failRemove;

        @Override public String token() { return token; }

        @Override public boolean removeToken() {
            if (failRemove) return false;
            token = null;
            return true;
        }

        @Override public boolean migrated() { return migrated; }

        @Override public void markMigrated() { migrated = true; }

        @Override public boolean hasUser() { return user; }
    }

    @Test
    public void legacyTokenMovesToTheVaultAndThePlainCopyIsRemoved() {
        FakeVault vault = new FakeVault();
        FakeLegacy legacy = new FakeLegacy();
        legacy.token = "secret-token";
        SessionCredentials credentials = new SessionCredentials(vault, legacy);
        assertEquals(SessionCredentials.Migration.MIGRATED, credentials.migrate());
        assertEquals("secret-token", vault.value);
        assertNull(legacy.token);
        assertTrue(legacy.migrated);
        assertEquals("secret-token", credentials.token());
    }

    @Test
    public void migratingTwiceIsANoOp() {
        FakeVault vault = new FakeVault();
        FakeLegacy legacy = new FakeLegacy();
        legacy.token = "secret-token";
        SessionCredentials credentials = new SessionCredentials(vault, legacy);
        credentials.migrate();
        assertEquals(SessionCredentials.Migration.NOTHING_TO_MIGRATE, credentials.migrate());
        assertEquals(1, vault.writes);
        assertEquals("secret-token", credentials.token());
    }

    @Test
    public void aFailedSecureWriteKeepsTheLegacyCredentialAndIsNotReportedAsMigrated() {
        FakeVault vault = new FakeVault();
        vault.failWrites = true;
        FakeLegacy legacy = new FakeLegacy();
        legacy.token = "secret-token";
        SessionCredentials credentials = new SessionCredentials(vault, legacy);
        assertEquals(SessionCredentials.Migration.SECURE_STORE_UNAVAILABLE, credentials.migrate());
        assertEquals("secret-token", legacy.token);
        assertFalse(legacy.migrated);
        assertEquals("secret-token", credentials.token());  // still signed in, not silently lost
    }

    @Test
    public void aMismatchedReadBackKeepsTheLegacyCredential() {
        FakeVault vault = new FakeVault();
        vault.corruptReads = true;
        FakeLegacy legacy = new FakeLegacy();
        legacy.token = "secret-token";
        assertEquals(SessionCredentials.Migration.VERIFY_FAILED, new SessionCredentials(vault, legacy).migrate());
        assertEquals("secret-token", legacy.token);
        assertFalse(legacy.migrated);
    }

    @Test
    public void aTokenWrittenByAnOlderAppVersionAfterMigrationWins() {
        FakeVault vault = new FakeVault();
        FakeLegacy legacy = new FakeLegacy();
        vault.value = "old-session";
        legacy.migrated = true;
        legacy.token = "newer-login-from-rollback-build";
        SessionCredentials credentials = new SessionCredentials(vault, legacy);
        assertEquals("newer-login-from-rollback-build", credentials.token());
        assertNull(legacy.token);
    }

    @Test
    public void signOutClearsBothLocationsAndAnOlderVersionsSignOutIsHonoured() throws Exception {
        FakeVault vault = new FakeVault();
        FakeLegacy legacy = new FakeLegacy();
        SessionCredentials credentials = new SessionCredentials(vault, legacy);
        credentials.setToken("secret-token");
        legacy.token = "stale";
        credentials.clear();
        assertNull(vault.value);
        assertNull(legacy.token);
        credentials.setToken("secret-token");
        legacy.user = false;  // an older version signed out: it only removed the legacy keys
        assertNull(credentials.token());
        assertNull(vault.value);
    }

    @Test
    public void storingNeverFallsBackToPlainStorage() {
        FakeVault vault = new FakeVault();
        vault.failWrites = true;
        FakeLegacy legacy = new FakeLegacy();
        SessionCredentials credentials = new SessionCredentials(vault, legacy);
        assertThrows(IllegalStateException.class, () -> credentials.setToken("secret-token"));
        assertNull(legacy.token);
    }
}
