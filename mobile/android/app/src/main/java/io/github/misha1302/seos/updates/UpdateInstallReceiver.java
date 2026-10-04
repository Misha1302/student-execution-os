package io.github.misha1302.seos.updates;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageInstaller;
import android.os.Build;
import android.util.Log;

/** Receives PackageInstaller's explicit callback and opens only its OS-owned confirmation UI. */
public class UpdateInstallReceiver extends BroadcastReceiver {
    static final String PREFS = UpdateInstallerState.PREFS;
    // In-process only: the OS confirmation intent cannot be persisted. It lets
    // continueInstaller() reopen the exact OS-owned prompt while this process lives.
    private static volatile Intent pendingConfirmation;
    private static volatile int pendingConfirmationSession = -1;

    static Intent pendingConfirmation(int sessionId) {
        return pendingConfirmationSession == sessionId ? pendingConfirmation : null;
    }

    @Override public void onReceive(Context context, Intent intent) {
        int status = intent.getIntExtra(PackageInstaller.EXTRA_STATUS, PackageInstaller.STATUS_FAILURE);
        int sessionId = intent.getIntExtra(PackageInstaller.EXTRA_SESSION_ID, -1);
        String message = intent.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE);
        UpdateInstallerState state = new UpdateInstallerState(context);
        if (state.get().sessionId != sessionId) {
            Log.w("SeosUpdate", "Ignoring stale PackageInstaller callback for session " + sessionId);
            return;
        }
        if (status == PackageInstaller.STATUS_PENDING_USER_ACTION) {
            Intent confirmation = Build.VERSION.SDK_INT >= 33
                    ? intent.getParcelableExtra(Intent.EXTRA_INTENT, Intent.class)
                    : intent.getParcelableExtra(Intent.EXTRA_INTENT);
            if (confirmation != null) {
                confirmation.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                if (!state.callback(sessionId, "USER_ACTION_REQUIRED", "", message)) return;
                pendingConfirmation = confirmation;
                pendingConfirmationSession = sessionId;
                try {
                    context.startActivity(confirmation);
                } catch (RuntimeException failure) {
                    state.callback(sessionId, "FAILED", "PERMISSION_REQUIRED", "Android confirmation could not be opened");
                }
            } else state.callback(sessionId, "FAILED", "INSTALLER_FAILED", "Android confirmation is missing");
        } else if (status == PackageInstaller.STATUS_SUCCESS) {
            state.callback(sessionId, "INSTALLED", "", message);
        } else {
            state.callback(sessionId, "FAILED", code(status), message);
        }
    }

    private static String code(int status) {
        if (status == PackageInstaller.STATUS_FAILURE_ABORTED) return "INSTALL_CANCELLED";
        if (status == PackageInstaller.STATUS_FAILURE_BLOCKED) return "PERMISSION_REQUIRED";
        if (status == PackageInstaller.STATUS_FAILURE_CONFLICT) return "PACKAGE_IDENTITY_MISMATCH";
        if (status == PackageInstaller.STATUS_FAILURE_STORAGE) return "INSUFFICIENT_DISK";
        return "INSTALLER_FAILED";
    }
}
