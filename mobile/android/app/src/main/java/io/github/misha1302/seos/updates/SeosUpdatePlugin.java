package io.github.misha1302.seos.updates;

import android.app.PendingIntent;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageInstaller;
import android.net.Uri;
import android.os.Build;
import android.os.storage.StorageManager;
import android.provider.Settings;
import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;
import io.github.misha1302.seos.BuildConfig;
import java.io.BufferedInputStream;
import java.io.BufferedOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URI;
import java.net.URL;
import java.nio.channels.FileChannel;
import java.nio.channels.FileLock;
import java.io.RandomAccessFile;
import java.security.MessageDigest;
import java.util.Locale;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import org.json.JSONObject;

/** Android mechanics behind the project-owned update service. It never selects a version. */
@CapacitorPlugin(name = "SeosUpdate")
public class SeosUpdatePlugin extends Plugin {
    private final ExecutorService worker = Executors.newSingleThreadExecutor();
    private static final long MIB = 1024L * 1024L;

    @Override protected void handleOnDestroy() {
        worker.shutdownNow();
        super.handleOnDestroy();
    }

    @PluginMethod public void configuration(PluginCall call) {
        JSObject out = new JSObject();
        out.put("enabled", !BuildConfig.SEOS_UPDATE_POLICY_URL_TEMPLATE.isBlank()
                && !"{}".equals(BuildConfig.SEOS_UPDATE_TRUST_KEYS_JSON.trim()));
        out.put("debug", BuildConfig.DEBUG);
        out.put("policyUrlTemplate", BuildConfig.SEOS_UPDATE_POLICY_URL_TEMPLATE);
        out.put("betaChannelAvailable", BuildConfig.SEOS_UPDATE_BETA_AVAILABLE);
        out.put("platform", "android");
        out.put("architecture", architecture());
        out.put("sdk", Build.VERSION.SDK_INT);
        out.put("versionName", BuildConfig.VERSION_NAME);
        out.put("buildNumber", BuildConfig.VERSION_CODE);
        try { out.put("trustedKeyIds", new JSONObject(BuildConfig.SEOS_UPDATE_TRUST_KEYS_JSON).names()); }
        catch (Exception ignored) { out.put("trustedKeyIds", null); }
        call.resolve(out);
    }

    @PluginMethod public void verifyPolicy(PluginCall call) {
        worker.execute(() -> {
            try {
                UpdateTrust.verify(BuildConfig.SEOS_UPDATE_TRUST_KEYS_JSON, required(call, "keyId"),
                        required(call, "algorithm"), required(call, "signature"), required(call, "canonicalPolicy"));
                call.resolve(new JSObject().put("verified", true));
            } catch (Exception invalid) { call.reject("Update policy signature is invalid", "SIGNATURE_INVALID", invalid); }
        });
    }

    @PluginMethod public void download(PluginCall call) {
        worker.execute(() -> {
            File partial = null;
            try {
                String version = safeVersion(required(call, "version"));
                long build = requiredLong(call, "buildNumber");
                long expectedSize = requiredSize(call);
                String expectedHash = requiredHash(call);
                URL initial = validatedUrl(required(call, "url"));
                File directory = new File(getContext().getCacheDir(), "updates");
                if (!directory.exists() && !directory.mkdirs()) throw new UpdateFailure("PERMISSION_REQUIRED", "Cannot create update cache");
                if (availableBytes(directory) < expectedSize * 2L + 16L * MIB) throw new UpdateFailure("INSUFFICIENT_DISK", "Not enough free space for the update");
                File target = new File(directory, "student-execution-os-" + version + "-" + build + ".apk");
                partial = new File(target.getPath() + ".partial");
                try (Lock ignored = lock(directory)) {
                    if (target.isFile()) {
                        try {
                            UpdateFiles.verify(getContext(), target, version, build, expectedSize, expectedHash, required(call, "packageName"));
                            call.resolve(downloadResult(target)); return;
                        } catch (IOException stale) { if (!target.delete()) throw new UpdateFailure("PERMISSION_REQUIRED", "Cannot replace stale update cache"); }
                    }
                    if (partial.exists() && !partial.delete()) throw new UpdateFailure("PERMISSION_REQUIRED", "Cannot clear partial update");
                    fetch(initial, partial, expectedSize, expectedHash);
                    UpdateFiles.verify(getContext(), partial, version, build, expectedSize, expectedHash, required(call, "packageName"));
                    if (!partial.renameTo(target)) throw new UpdateFailure("PERMISSION_REQUIRED", "Cannot finalize update atomically");
                    call.resolve(downloadResult(target));
                }
            } catch (UpdateFailure failure) {
                if (partial != null) partial.delete();
                call.reject(failure.getMessage(), failure.code, failure);
            } catch (IOException failure) {
                if (partial != null) partial.delete();
                String message = failure.getMessage() == null ? "" : failure.getMessage();
                String code = message.contains("hash") || message.contains("size") ? "HASH_MISMATCH"
                        : message.contains("APK") || message.contains("package") || message.contains("version") ? "PACKAGE_INVALID"
                        : "NETWORK_ERROR";
                call.reject(message.isBlank() ? "Update download failed" : message, code, failure);
            } catch (Exception failure) {
                if (partial != null) partial.delete();
                call.reject("Update download failed", "DOWNLOAD_FAILED", failure);
            }
        });
    }

    @PluginMethod public void verifyDownloaded(PluginCall call) {
        worker.execute(() -> verifyCall(call, false));
    }

    @PluginMethod public void apply(PluginCall call) {
        worker.execute(() -> verifyCall(call, true));
    }

    @PluginMethod public void getState(PluginCall call) {
        SharedPreferences state = getContext().getSharedPreferences(UpdateInstallReceiver.PREFS, android.content.Context.MODE_PRIVATE);
        JSObject out = new JSObject();
        out.put("state", state.getString("state", "IDLE"));
        out.put("code", state.getString("code", ""));
        out.put("message", state.getString("message", ""));
        out.put("targetVersion", state.getString("target_version", ""));
        out.put("targetBuild", state.getLong("target_build", 0));
        out.put("updatedAt", state.getLong("updated_at", 0));
        call.resolve(out);
    }

    @PluginMethod public void openInstallPermission(PluginCall call) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            Intent intent = new Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                    Uri.parse("package:" + getContext().getPackageName())).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            try { getContext().startActivity(intent); }
            catch (RuntimeException missing) {
                getContext().startActivity(new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                        Uri.parse("package:" + getContext().getPackageName())).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
            }
        }
        call.resolve();
    }

    private void verifyCall(PluginCall call, boolean install) {
        try {
            File apk = new File(required(call, "path"));
            String version = safeVersion(required(call, "version"));
            long build = requiredLong(call, "buildNumber");
            long size = requiredSize(call);
            String hash = requiredHash(call);
            String packageName = required(call, "packageName");
            File expectedRoot = new File(getContext().getCacheDir(), "updates").getCanonicalFile();
            if (!apk.getCanonicalFile().getParentFile().equals(expectedRoot)) throw new UpdateFailure("PACKAGE_INVALID", "Update is outside the private cache");
            try (Lock ignored = lock(expectedRoot)) {
                UpdateFiles.verify(getContext(), apk, version, build, size, hash, packageName);
                if (!install) { call.resolve(downloadResult(apk)); return; }
                commit(apk, packageName, size, version, build);
                call.resolve(new JSObject().put("status", "COMMITTED").put("userActionMayBeRequired", true));
            }
        } catch (UpdateFailure failure) { call.reject(failure.getMessage(), failure.code, failure); }
        catch (IOException failure) {
            String code = failure.getMessage() != null && failure.getMessage().contains("hash") ? "HASH_MISMATCH" : "PACKAGE_INVALID";
            call.reject(failure.getMessage(), code, failure);
        } catch (Exception failure) { call.reject("Package apply failed", "INSTALLER_FAILED", failure); }
    }

    private void commit(File apk, String packageName, long size, String version, long build) throws Exception {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O && !getContext().getPackageManager().canRequestPackageInstalls()) {
            throw new UpdateFailure("PERMISSION_REQUIRED", "Allow this app to install its signed update");
        }
        PackageInstaller installer = getContext().getPackageManager().getPackageInstaller();
        for (PackageInstaller.SessionInfo session : installer.getMySessions()) {
            // Finished sessions can remain visible briefly. Only an active session
            // competes with this process for the platform install boundary.
            if (session.isActive()) {
                throw new UpdateFailure("UPDATE_LOCKED", "An Android install session is already active");
            }
        }
        PackageInstaller.SessionParams params = new PackageInstaller.SessionParams(PackageInstaller.SessionParams.MODE_FULL_INSTALL);
        params.setAppPackageName(packageName); params.setSize(size);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) params.setRequireUserAction(PackageInstaller.SessionParams.USER_ACTION_REQUIRED);
        int id = installer.createSession(params);
        boolean committed = false;
        try (PackageInstaller.Session session = installer.openSession(id)) {
            try (InputStream input = new BufferedInputStream(new java.io.FileInputStream(apk));
                 OutputStream sessionOutput = session.openWrite("base.apk", 0, size);
                 BufferedOutputStream output = new BufferedOutputStream(sessionOutput)) {
                byte[] buffer = new byte[128 * 1024]; int count;
                while ((count = input.read(buffer)) >= 0) if (count > 0) output.write(buffer, 0, count);
                // PackageInstaller requires the exact stream returned by
                // openWrite(), not the buffering wrapper, for fsync().
                output.flush(); session.fsync(sessionOutput);
            }
            Intent callback = new Intent(getContext(), UpdateInstallReceiver.class).setAction("io.github.misha1302.seos.UPDATE_RESULT");
            int flags = PendingIntent.FLAG_UPDATE_CURRENT;
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) flags |= PendingIntent.FLAG_MUTABLE;
            PendingIntent pending = PendingIntent.getBroadcast(getContext(), id, callback, flags);
            SharedPreferences.Editor state = getContext().getSharedPreferences(UpdateInstallReceiver.PREFS, android.content.Context.MODE_PRIVATE).edit()
                    .putString("state", "COMMITTED").putString("target_version", version).putLong("target_build", build)
                    .putLong("updated_at", System.currentTimeMillis());
            state.apply();
            session.commit(pending.getIntentSender()); committed = true;
        } finally { if (!committed) installer.abandonSession(id); }
    }

    private void fetch(URL initial, File partial, long expectedSize, String expectedHash) throws Exception {
        URL current = initial;
        for (int redirect = 0; redirect <= 5; redirect++) {
            HttpURLConnection connection = (HttpURLConnection) current.openConnection();
            connection.setInstanceFollowRedirects(false); connection.setConnectTimeout(15_000); connection.setReadTimeout(30_000);
            connection.setRequestProperty("Accept", "application/vnd.android.package-archive,application/octet-stream");
            int status = connection.getResponseCode();
            if (status >= 300 && status < 400) {
                String location = connection.getHeaderField("Location"); connection.disconnect();
                if (location == null || redirect == 5) throw new UpdateFailure("NETWORK_ERROR", "Unsafe or excessive artifact redirect");
                current = validatedUrl(new URL(current, location).toString());
                continue;
            }
            if (status != 200) { connection.disconnect(); throw new UpdateFailure("NETWORK_ERROR", "Artifact HTTP " + status); }
            long headerSize = connection.getContentLengthLong();
            if (headerSize > 0 && headerSize != expectedSize) { connection.disconnect(); throw new UpdateFailure("HASH_MISMATCH", "Artifact size header mismatch"); }
            MessageDigest digest = MessageDigest.getInstance("SHA-256"); long total = 0; long lastReport = 0;
            try (InputStream input = new BufferedInputStream(connection.getInputStream());
                 FileOutputStream file = new FileOutputStream(partial); BufferedOutputStream output = new BufferedOutputStream(file)) {
                byte[] buffer = new byte[128 * 1024]; int count;
                while ((count = input.read(buffer)) >= 0) if (count > 0) {
                    total += count; if (total > expectedSize) throw new UpdateFailure("HASH_MISMATCH", "Artifact exceeds signed size");
                    output.write(buffer, 0, count); digest.update(buffer, 0, count);
                    if (total - lastReport >= 256 * 1024 || total == expectedSize) {
                        lastReport = total; notifyListeners("downloadProgress", new JSObject().put("bytes", total).put("total", expectedSize).put("fraction", (double) total / expectedSize));
                    }
                }
                output.flush(); file.getFD().sync();
            } finally { connection.disconnect(); }
            if (total != expectedSize || !constantTimeHex(hex(digest.digest()), expectedHash)) throw new UpdateFailure("HASH_MISMATCH", "Downloaded artifact does not match signed policy");
            return;
        }
    }

    private URL validatedUrl(String value) throws Exception {
        URI uri = URI.create(value); String scheme = uri.getScheme();
        String host = uri.getHost();
        boolean localHttp = BuildConfig.DEBUG && "http".equalsIgnoreCase(scheme)
                && ("127.0.0.1".equals(host) || "localhost".equalsIgnoreCase(host) || "10.0.2.2".equals(host));
        if (uri.getHost() == null || uri.getUserInfo() != null || uri.getFragment() != null
                || !("https".equalsIgnoreCase(scheme) || localHttp)) {
            throw new UpdateFailure("METADATA_INVALID", "Artifact URL is not allowed");
        }
        return uri.toURL();
    }

    private Lock lock(File directory) throws IOException, UpdateFailure {
        File file = new File(directory, ".update.lock");
        RandomAccessFile access = new RandomAccessFile(file, "rw");
        FileChannel channel = access.getChannel();
        try {
            FileLock lock = channel.tryLock();
            if (lock == null) { access.close(); throw new UpdateFailure("UPDATE_LOCKED", "Another process is updating the app"); }
            return new Lock(access, channel, lock);
        } catch (java.nio.channels.OverlappingFileLockException busy) {
            access.close(); throw new UpdateFailure("UPDATE_LOCKED", "Another update operation is active");
        } catch (IOException | UpdateFailure failure) {
            access.close(); throw failure;
        }
    }

    private long availableBytes(File directory) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            StorageManager storage = getContext().getSystemService(StorageManager.class);
            if (storage != null) {
                try { return storage.getAllocatableBytes(StorageManager.UUID_DEFAULT); }
                catch (IOException | SecurityException ignored) { /* fall through for API 24/25 semantics */ }
            }
        }
        return directory.getUsableSpace();
    }

    private static String required(PluginCall call, String name) throws UpdateFailure {
        String value = call.getString(name);
        if (value == null || value.isBlank()) throw new UpdateFailure("METADATA_INVALID", name + " is required");
        return value;
    }
    private static long requiredLong(PluginCall call, String name) throws UpdateFailure {
        Long value = call.getLong(name);
        if (value == null || value <= 0) throw new UpdateFailure("METADATA_INVALID", name + " is invalid");
        return value;
    }
    private static long requiredSize(PluginCall call) throws UpdateFailure {
        long value = requiredLong(call, "sizeBytes");
        if (value > 500L * MIB) throw new UpdateFailure("METADATA_INVALID", "sizeBytes exceeds the safety bound");
        return value;
    }
    private static String requiredHash(PluginCall call) throws UpdateFailure {
        String value = required(call, "sha256");
        if (!value.matches("[0-9a-f]{64}")) throw new UpdateFailure("METADATA_INVALID", "sha256 is invalid");
        return value;
    }
    private static String safeVersion(String value) throws UpdateFailure {
        if (!value.matches("[0-9A-Za-z.+-]{1,80}")) throw new UpdateFailure("METADATA_INVALID", "version is unsafe");
        return value;
    }
    private static JSObject downloadResult(File file) { return new JSObject().put("path", file.getAbsolutePath()).put("sizeBytes", file.length()); }
    private static String architecture() {
        for (String abi : Build.SUPPORTED_ABIS) {
            if ("arm64-v8a".equals(abi)) return "arm64";
            if ("x86_64".equals(abi)) return "x64";
        }
        return "universal";
    }
    private static String hex(byte[] bytes) { StringBuilder out = new StringBuilder(); for (byte value : bytes) out.append(String.format(Locale.ROOT, "%02x", value & 0xff)); return out.toString(); }
    private static boolean constantTimeHex(String left, String right) { if (left.length() != right.length()) return false; int diff = 0; for (int i = 0; i < left.length(); i++) diff |= left.charAt(i) ^ right.charAt(i); return diff == 0; }

    private static final class Lock implements AutoCloseable {
        final RandomAccessFile access; final FileChannel channel; final FileLock lock;
        Lock(RandomAccessFile access, FileChannel channel, FileLock lock) { this.access = access; this.channel = channel; this.lock = lock; }
        @Override public void close() throws IOException { lock.release(); channel.close(); access.close(); }
    }
    private static final class UpdateFailure extends Exception {
        final String code;
        UpdateFailure(String code, String message) { super(message); this.code = code; }
    }
}
