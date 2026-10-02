package io.github.misha1302.seos.storage;

/** Where a session credential is kept. Implementations never log or expose the value. */
public interface CredentialVault {
    /** The stored value, or null; throws when the value exists but cannot be read. */
    String read() throws Exception;

    void write(String value) throws Exception;

    void clear();
}
