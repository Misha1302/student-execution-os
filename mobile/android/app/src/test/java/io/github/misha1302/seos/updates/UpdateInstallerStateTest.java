package io.github.misha1302.seos.updates;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

public class UpdateInstallerStateTest {
    private static final class MemoryBackend implements UpdateInstallerState.Backend {
        UpdateInstallerState.Snapshot value = new UpdateInstallerState.Snapshot(
                -1, "IDLE", "", "", "", 0, "", 0);
        boolean writable = true;
        @Override public UpdateInstallerState.Snapshot load() { return value; }
        @Override public boolean save(UpdateInstallerState.Snapshot next) {
            if (!writable) return false;
            value = next;
            return true;
        }
    }

    @Test public void sessionIdentityAndTargetArePersistedBeforeSubmission() {
        MemoryBackend backend = new MemoryBackend();
        UpdateInstallerState state = new UpdateInstallerState(backend);
        assertTrue(state.begin(41, "1.2.3", 123, "a".repeat(64)));
        assertEquals(41, state.get().sessionId);
        assertEquals("1.2.3", state.get().targetVersion);
        assertEquals(123, state.get().targetBuild);
        assertEquals("a".repeat(64), state.get().targetSha256);
        assertEquals("PREPARING", state.get().state);
    }

    @Test public void staleCallbackCannotMutateCurrentSession() {
        MemoryBackend backend = new MemoryBackend();
        UpdateInstallerState state = new UpdateInstallerState(backend);
        state.begin(42, "1.2.3", 123, "b".repeat(64));
        state.markSubmitting(42);
        state.markCommitted(42);
        assertFalse(state.callback(41, "FAILED", "INSTALL_CANCELLED", "stale"));
        assertEquals("COMMITTED", state.get().state);
    }

    @Test public void matchingCallbackAdvancesAndTerminalStateCannotBeOverwritten() {
        MemoryBackend backend = new MemoryBackend();
        UpdateInstallerState state = new UpdateInstallerState(backend);
        state.begin(43, "1.2.3", 123, "c".repeat(64));
        state.markSubmitting(43);
        state.markCommitted(43);
        assertTrue(state.callback(43, "INSTALLED", "", "ok"));
        assertEquals("INSTALLED", state.get().state);
        assertFalse(state.callback(43, "FAILED", "INSTALLER_FAILED", "late"));
        assertEquals("INSTALLED", state.get().state);
    }

    @Test public void commitFailureNeverLeavesCommitted() {
        MemoryBackend backend = new MemoryBackend();
        UpdateInstallerState state = new UpdateInstallerState(backend);
        state.begin(44, "1.2.3", 123, "d".repeat(64));
        state.markSubmitting(44);
        state.fail(44, "INSTALLER_FAILED", "commit threw");
        assertEquals("FAILED", state.get().state);
        assertFalse(state.markCommitted(44));
        assertEquals("FAILED", state.get().state);
    }

    @Test public void installedPackageReconcilesEveryPendingStateIncludingManualRecovery() {
        for (String pending : new String[] {"PREPARING", "SUBMITTING", "COMMITTED", "USER_ACTION_REQUIRED"}) {
            MemoryBackend backend = new MemoryBackend();
            backend.value = new UpdateInstallerState.Snapshot(45, pending, "", "", "1.2.3", 123, "e".repeat(64), 0);
            UpdateInstallerState state = new UpdateInstallerState(backend);
            assertFalse(state.reconcileInstalledBuild(122));
            assertEquals(pending, state.get().state);
            assertTrue(state.reconcileInstalledBuild(123));
            assertEquals("INSTALLED", state.get().state);
            assertEquals(45, state.get().sessionId);
            assertEquals("e".repeat(64), state.get().targetSha256);
            assertFalse(state.callback(45, "FAILED", "INSTALLER_FAILED", "late"));
        }
    }

    @Test public void failedInstalledStateWriteRetainsOwnershipForRetry() {
        MemoryBackend backend = new MemoryBackend();
        UpdateInstallerState state = new UpdateInstallerState(backend);
        state.begin(46, "1.2.3", 123, "f".repeat(64));
        backend.writable = false;
        assertFalse(state.reconcileInstalledBuild(123));
        assertEquals("PREPARING", state.get().state);
        backend.writable = true;
        assertTrue(state.reconcileInstalledBuild(124));
        assertEquals("INSTALLED", state.get().state);
    }

    @Test public void installedBuildEvidenceDoesNotRewriteIdleOrTerminalStates() {
        for (String terminal : new String[] {"IDLE", "FAILED", "INSTALLED"}) {
            MemoryBackend backend = new MemoryBackend();
            backend.value = new UpdateInstallerState.Snapshot(47, terminal, "", "", "1.2.3", 123, "a".repeat(64), 0);
            UpdateInstallerState state = new UpdateInstallerState(backend);
            assertFalse(state.reconcileInstalledBuild(124));
            assertEquals(terminal, state.get().state);
        }
    }
}
