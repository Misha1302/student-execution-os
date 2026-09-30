package io.github.misha1302.seos.updates;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;
import static org.junit.Assert.fail;

import com.getcapacitor.JSObject;
import org.junit.Test;

public class SeosUpdatePluginNumberTest {
    @Test
    public void jsonIntegerMetadataIsAcceptedAsLong() throws Exception {
        JSObject data = new JSObject("{\"buildNumber\":4,\"sizeBytes\":6027219}");
        assertTrue(data.opt("buildNumber") instanceof Integer);
        assertTrue(data.opt("sizeBytes") instanceof Integer);
        assertEquals(4L, SeosUpdatePlugin.positiveLong(data.opt("buildNumber"), "buildNumber"));
        assertEquals(6027219L, SeosUpdatePlugin.positiveLong(data.opt("sizeBytes"), "sizeBytes"));
    }

    @Test
    public void integralLongAndDoubleMetadataIsAccepted() {
        assertEquals(2147483648L, SeosUpdatePlugin.positiveLong(2147483648L, "value"));
        assertEquals(50L, SeosUpdatePlugin.positiveLong(50.0d, "value"));
    }

    @Test
    public void invalidNumericShapesAreRejected() {
        reject(null);
        reject("4");
        reject(0);
        reject(-1);
        reject(4.5d);
        reject(Double.NaN);
        reject(Double.POSITIVE_INFINITY);
    }

    private static void reject(Object value) {
        try {
            SeosUpdatePlugin.positiveLong(value, "value");
            fail("expected invalid update number");
        } catch (IllegalArgumentException expected) {
            assertEquals("value is invalid", expected.getMessage());
        }
    }
}
