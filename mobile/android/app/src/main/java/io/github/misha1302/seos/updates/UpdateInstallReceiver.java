package io.github.misha1302.seos.updates;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageInstaller;
import android.os.Build;

/** Receives PackageInstaller's explicit callback and opens only its OS-owned confirmation UI. */
public class UpdateInstallReceiver extends BroadcastReceiver {
    static final String PREFS = "seos-update-installer";

    @Override public void onReceive(Context context, Intent intent) {
        int status = intent.getIntExtra(PackageInstaller.EXTRA_STATUS, PackageInstaller.STATUS_FAILURE);
        SharedPreferences.Editor state = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
                .putInt("status", status)
                .putString("message", intent.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE))
                .putLong("updated_at", System.currentTimeMillis());
        if (status == PackageInstaller.STATUS_PENDING_USER_ACTION) {
            Intent confirmation = Build.VERSION.SDK_INT >= 33
                    ? intent.getParcelableExtra(Intent.EXTRA_INTENT, Intent.class)
                    : intent.getParcelableExtra(Intent.EXTRA_INTENT);
            if (confirmation != null) {
                confirmation.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                try {
                    context.startActivity(confirmation);
                    state.putString("state", "USER_ACTION_REQUIRED");
                } catch (RuntimeException failure) {
                    state.putString("state", "FAILED").putString("code", "PERMISSION_REQUIRED");
                }
            } else state.putString("state", "FAILED").putString("code", "INSTALLER_FAILED");
        } else if (status == PackageInstaller.STATUS_SUCCESS) {
            state.putString("state", "INSTALLED").putString("code", "");
        } else {
            state.putString("state", "FAILED").putString("code", code(status));
        }
        state.apply();
    }

    private static String code(int status) {
        if (status == PackageInstaller.STATUS_FAILURE_ABORTED) return "INSTALL_CANCELLED";
        if (status == PackageInstaller.STATUS_FAILURE_BLOCKED) return "PERMISSION_REQUIRED";
        if (status == PackageInstaller.STATUS_FAILURE_CONFLICT) return "PACKAGE_IDENTITY_MISMATCH";
        if (status == PackageInstaller.STATUS_FAILURE_STORAGE) return "INSUFFICIENT_DISK";
        return "INSTALLER_FAILED";
    }
}
