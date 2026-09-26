package io.github.misha1302.seos.updates;

import android.content.Context;
import android.content.pm.PackageInfo;
import android.content.pm.PackageManager;
import android.content.pm.Signature;
import android.os.Build;
import java.io.File;
import java.io.FileInputStream;
import java.io.IOException;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.Arrays;
import java.util.HashSet;
import java.util.Locale;
import java.util.Set;

/** Project-level verification performed again immediately before PackageInstaller. */
final class UpdateFiles {
    private UpdateFiles() {}

    static String sha256(File file) throws IOException {
        try {
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            byte[] buffer = new byte[128 * 1024];
            try (FileInputStream input = new FileInputStream(file)) {
                int count;
                while ((count = input.read(buffer)) >= 0) if (count > 0) digest.update(buffer, 0, count);
            }
            StringBuilder out = new StringBuilder(64);
            for (byte value : digest.digest()) out.append(String.format(Locale.ROOT, "%02x", value & 0xff));
            return out.toString();
        } catch (NoSuchAlgorithmException impossible) { throw new AssertionError(impossible); }
    }

    static void verify(Context context, File apk, String expectedVersion, long expectedBuild,
                       long expectedSize, String expectedHash, String expectedPackage) throws IOException {
        if (!apk.isFile()) throw new IOException("downloaded APK is missing");
        if (apk.length() != expectedSize) throw new IOException("artifact size mismatch");
        if (!constantTimeEquals(sha256(apk), expectedHash)) throw new IOException("artifact hash mismatch");

        PackageManager manager = context.getPackageManager();
        int flags = Build.VERSION.SDK_INT >= Build.VERSION_CODES.P
                ? PackageManager.GET_SIGNING_CERTIFICATES : PackageManager.GET_SIGNATURES;
        PackageInfo archive = manager.getPackageArchiveInfo(apk.getAbsolutePath(), flags);
        if (archive == null) throw new IOException("artifact is not a readable APK");
        if (!expectedPackage.equals(archive.packageName) || !context.getPackageName().equals(archive.packageName)) {
            throw new IOException("APK package identity mismatch");
        }
        long archiveBuild = Build.VERSION.SDK_INT >= Build.VERSION_CODES.P ? archive.getLongVersionCode() : archive.versionCode;
        if (archiveBuild != expectedBuild || !expectedVersion.equals(archive.versionName)) throw new IOException("APK version/build mismatch");

        try {
            PackageInfo installed = manager.getPackageInfo(context.getPackageName(), flags);
            long currentBuild = Build.VERSION.SDK_INT >= Build.VERSION_CODES.P ? installed.getLongVersionCode() : installed.versionCode;
            if (archiveBuild <= currentBuild) throw new IOException("binary downgrade or same-build replacement is forbidden");
            if (!certificates(installed).equals(certificates(archive))) throw new IOException("APK signing identity mismatch");
        } catch (PackageManager.NameNotFoundException impossible) { throw new IOException("installed package identity unavailable", impossible); }
    }

    private static Set<String> certificates(PackageInfo info) throws IOException {
        Signature[] signatures;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            if (info.signingInfo == null) throw new IOException("APK signing information missing");
            signatures = info.signingInfo.hasMultipleSigners()
                    ? info.signingInfo.getApkContentsSigners() : info.signingInfo.getSigningCertificateHistory();
        } else signatures = info.signatures;
        if (signatures == null || signatures.length == 0) throw new IOException("APK has no signing certificate");
        Set<String> result = new HashSet<>();
        for (Signature signature : signatures) result.add(Arrays.toString(signature.toByteArray()));
        return result;
    }

    private static boolean constantTimeEquals(String left, String right) {
        if (left == null || right == null || left.length() != right.length()) return false;
        int difference = 0;
        for (int i = 0; i < left.length(); i++) difference |= left.charAt(i) ^ right.charAt(i);
        return difference == 0;
    }
}
