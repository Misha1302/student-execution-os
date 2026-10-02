package io.github.misha1302.seos.storage;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

/**
 * The web client's durable device storage on Android (device-storage.js):
 * the offline operation queue in SQLite and the session token in the Android Keystore.
 * Calls run on the plugin's background thread; a call resolves only after the write is
 * committed. Values are never logged.
 */
@CapacitorPlugin(name = "SeosStorage")
public class SeosStoragePlugin extends Plugin {
    private OfflineQueueDb queue() { return OfflineQueueDb.get(getContext()); }

    private SessionCredentials credentials() { return SessionCredentials.of(getContext()); }

    @PluginMethod
    public void queueLoadAll(PluginCall call) {
        try {
            JSObject out = new JSObject();
            out.put("scopes", queue().loadAll());
            call.resolve(out);
        } catch (Exception failure) {
            call.reject("offline queue unreadable", "QUEUE_READ");
        }
    }

    @PluginMethod
    public void queueReplace(PluginCall call) {
        String scope = call.getString("scope");
        String items = call.getString("items");
        if (scope == null || items == null) {
            call.reject("scope and items are required", "QUEUE_ARGS");
            return;
        }
        try {
            queue().replace(scope, items);
            call.resolve();
        } catch (Exception failure) {
            call.reject("offline queue not saved", "QUEUE_WRITE");
        }
    }

    @PluginMethod
    public void queueImportLegacy(PluginCall call) {
        String scope = call.getString("scope");
        String items = call.getString("items");
        if (scope == null || items == null) {
            call.reject("scope and items are required", "QUEUE_ARGS");
            return;
        }
        try {
            int added = queue().importLegacy(scope, items);
            JSObject out = new JSObject();
            out.put("added", added);
            out.put("verified", queue().covers(scope, items));
            call.resolve(out);
        } catch (Exception failure) {
            call.reject("legacy queue not imported", "QUEUE_IMPORT");
        }
    }

    @PluginMethod
    public void metaGet(PluginCall call) {
        JSObject out = new JSObject();
        out.put("value", queue().meta(call.getString("key", "")));
        call.resolve(out);
    }

    @PluginMethod
    public void metaSet(PluginCall call) {
        queue().setMeta(call.getString("key", ""), call.getString("value", ""));
        call.resolve();
    }

    @PluginMethod
    public void credentialMigrate(PluginCall call) {
        JSObject out = new JSObject();
        out.put("result", credentials().migrate().name());
        call.resolve(out);
    }

    @PluginMethod
    public void credentialGet(PluginCall call) {
        JSObject out = new JSObject();
        out.put("value", credentials().token());
        call.resolve(out);
    }

    @PluginMethod
    public void credentialSet(PluginCall call) {
        try {
            credentials().setToken(call.getString("value"));
            call.resolve();
        } catch (Exception failure) {
            call.reject("secure credential storage is unavailable", "CREDENTIAL_WRITE");
        }
    }

    @PluginMethod
    public void credentialClear(PluginCall call) {
        credentials().clear();
        call.resolve();
    }
}
