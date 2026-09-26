package io.github.misha1302.seos.updates;

import android.util.Base64;
import com.google.crypto.tink.subtle.Ed25519Verify;
import java.nio.charset.StandardCharsets;
import java.security.GeneralSecurityException;
import org.json.JSONException;
import org.json.JSONObject;

/** Ed25519 verification over the publisher's canonical policy bytes. */
final class UpdateTrust {
    private UpdateTrust() {}

    static void verify(String trustedKeysJson, String keyId, String algorithm, String signatureB64,
                       String canonicalPolicy) throws GeneralSecurityException, JSONException {
        if (!"Ed25519".equals(algorithm)) throw new GeneralSecurityException("unsupported signature algorithm");
        JSONObject keys = new JSONObject(trustedKeysJson);
        if (!keys.has(keyId)) throw new GeneralSecurityException("untrusted update key id");
        byte[] publicKey = decode(keys.getString(keyId), 32, "public key");
        byte[] signature = decode(signatureB64, 64, "signature");
        new Ed25519Verify(publicKey).verify(signature, canonicalPolicy.getBytes(StandardCharsets.UTF_8));
    }

    private static byte[] decode(String encoded, int size, String name) throws GeneralSecurityException {
        final byte[] value;
        try { value = Base64.decode(encoded, Base64.DEFAULT); }
        catch (IllegalArgumentException invalid) { throw new GeneralSecurityException("invalid base64 " + name, invalid); }
        if (value.length != size) throw new GeneralSecurityException("invalid " + name + " size");
        return value;
    }
}
