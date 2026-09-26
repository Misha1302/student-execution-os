package io.github.misha1302.seos.updates;

import static org.junit.Assert.assertThrows;
import org.junit.Test;
import java.security.GeneralSecurityException;

/** RFC 8032 test vector; public material only. Runs on API 24+ with Tink. */
public class UpdateTrustDeviceTest {
    @Test public void verifiesEd25519AndRejectsMutation() throws Exception {
        String publicKey = "11qYAYKxCrfVS/7TyWQHOg7hcvPapiMlrwIaaPcHURo=";
        String signature = "5VZDAMNgrHKQhuLMgG6CioSHfx645dl02HPgZSJJAVVfuIIVkKM7rMYeOXAc+bRr0lv18FlbviRlUUFDjnoQCw==";
        UpdateTrust.verify("{\"rfc\":\"" + publicKey + "\"}", "rfc", "Ed25519", signature, "");
        assertThrows(GeneralSecurityException.class,
                () -> UpdateTrust.verify("{\"rfc\":\"" + publicKey + "\"}", "rfc", "Ed25519", signature, "mutated"));
    }
}
