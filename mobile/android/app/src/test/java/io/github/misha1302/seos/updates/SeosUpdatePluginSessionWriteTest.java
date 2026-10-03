package io.github.misha1302.seos.updates;

import static org.junit.Assert.assertArrayEquals;
import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertSame;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.OutputStream;
import java.util.ArrayList;
import java.util.List;
import org.junit.Test;

public class SeosUpdatePluginSessionWriteTest {
    /** Like PackageInstaller's FileBridgeOutputStream: any use after close hits a closed descriptor. */
    private static final class FileBridgeLikeStream extends OutputStream {
        final ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        final List<String> events = new ArrayList<>();
        int closes;

        private void open() throws IOException {
            if (closes > 0) throw new IOException("write failed: EBADF (Bad file descriptor)");
        }
        @Override public void write(int b) throws IOException { open(); bytes.write(b); }
        @Override public void write(byte[] b, int off, int len) throws IOException { open(); bytes.write(b, off, len); }
        @Override public void flush() throws IOException { open(); }
        @Override public void close() throws IOException { open(); closes++; events.add("close"); }
    }

    @Test
    public void sessionStreamIsSyncedThenClosedExactlyOnce() throws Exception {
        byte[] apk = new byte[300 * 1024 + 7];
        for (int i = 0; i < apk.length; i++) apk[i] = (byte) (i * 31);
        FileBridgeLikeStream session = new FileBridgeLikeStream();

        SeosUpdatePlugin.writeSession(new ByteArrayInputStream(apk), session, out -> {
            assertSame("fsync needs the exact openWrite() stream", session, out);
            session.events.add("fsync");
        });

        assertArrayEquals(apk, session.bytes.toByteArray());
        assertEquals(List.of("fsync", "close"), session.events);
        assertEquals(1, session.closes);
    }
}
