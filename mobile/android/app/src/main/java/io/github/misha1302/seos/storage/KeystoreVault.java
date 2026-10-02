package io.github.misha1302.seos.storage;

import android.content.Context;
import android.content.SharedPreferences;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.util.Base64;
import java.nio.charset.StandardCharsets;
import java.security.KeyStore;
import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;

/**
 * A credential encrypted with an AES-256-GCM key that is generated inside the Android
 * Keystore and can never be exported: only the ciphertext and its IV are stored in the
 * app's private preferences, the key never leaves the OS keystore. No key material is
 * hard-coded, derived or kept next to the ciphertext.
 */
public final class KeystoreVault implements CredentialVault {
    static final String KEYSTORE = "AndroidKeyStore";
    static final String ALIAS = "seos.session.credential.v1";
    static final String PREFS = "seos_secure_credentials";
    static final String VALUE = "token";
    private static final int TAG_BITS = 128;

    private final SharedPreferences prefs;
    private final String alias;

    public KeystoreVault(Context context) {
        this(context, PREFS, ALIAS);
    }

    KeystoreVault(Context context, String prefsName, String alias) {
        this.prefs = context.getSharedPreferences(prefsName, Context.MODE_PRIVATE);
        this.alias = alias;
    }

    @Override
    public String read() throws Exception {
        String stored = prefs.getString(VALUE, null);
        if (stored == null) return null;
        String[] parts = stored.split(":", 2);
        if (parts.length != 2) throw new IllegalStateException("unreadable secure credential");
        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
        cipher.init(Cipher.DECRYPT_MODE, key(false), new GCMParameterSpec(TAG_BITS, Base64.decode(parts[0], Base64.NO_WRAP)));
        return new String(cipher.doFinal(Base64.decode(parts[1], Base64.NO_WRAP)), StandardCharsets.UTF_8);
    }

    @Override
    public void write(String value) throws Exception {
        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
        cipher.init(Cipher.ENCRYPT_MODE, key(true));  // the Keystore chooses a fresh random IV
        byte[] sealed = cipher.doFinal(value.getBytes(StandardCharsets.UTF_8));
        String stored = Base64.encodeToString(cipher.getIV(), Base64.NO_WRAP) + ":" + Base64.encodeToString(sealed, Base64.NO_WRAP);
        if (!prefs.edit().putString(VALUE, stored).commit()) throw new IllegalStateException("secure credential not saved");
    }

    @Override
    public void clear() {
        prefs.edit().remove(VALUE).commit();
    }

    /** Whether the stored form is ciphertext (for tests; never returns the value). */
    boolean storedValueDiffersFrom(String plain) {
        String stored = prefs.getString(VALUE, null);
        return stored != null && !stored.contains(plain);
    }

    private SecretKey key(boolean create) throws Exception {
        KeyStore store = KeyStore.getInstance(KEYSTORE);
        store.load(null);
        KeyStore.Entry entry = store.getEntry(alias, null);
        if (entry instanceof KeyStore.SecretKeyEntry) return ((KeyStore.SecretKeyEntry) entry).getSecretKey();
        if (!create) throw new IllegalStateException("secure credential key is missing");
        KeyGenerator generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, KEYSTORE);
        generator.init(new KeyGenParameterSpec.Builder(alias, KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256)
                .setRandomizedEncryptionRequired(true)
                .build());
        return generator.generateKey();
    }
}
