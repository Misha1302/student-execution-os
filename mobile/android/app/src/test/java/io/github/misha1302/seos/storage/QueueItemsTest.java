package io.github.misha1302.seos.storage;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertThrows;
import static org.junit.Assert.assertTrue;

import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import org.json.JSONException;
import org.junit.Test;

public class QueueItemsTest {
    private static String item(String opId, String state) {
        return "{\"operation\":{\"op_id\":\"" + opId + "\",\"type\":\"task.start\",\"entity_id\":\"t1\",\"payload\":{}},"
                + "\"state\":\"" + state + "\"}";
    }

    @Test
    public void parsingKeepsOrderAndOpIdsExactly() throws JSONException {
        LinkedHashMap<String, String> items = QueueItems.parse("[" + item("op-b", "PENDING") + "," + item("op-a", "ACKED") + "]");
        assertEquals(List.of("op-b", "op-a"), List.copyOf(items.keySet()));
        assertTrue(items.get("op-b").contains("\"op_id\":\"op-b\""));
    }

    @Test
    public void malformedOrDuplicateItemsAreRejectedBeforeAnyWrite() {
        assertThrows(JSONException.class, () -> QueueItems.parse("[{\"state\":\"PENDING\"}]"));
        assertThrows(JSONException.class, () -> QueueItems.parse("[" + item("op-1", "PENDING") + "," + item("op-1", "PENDING") + "]"));
        assertThrows(JSONException.class, () -> QueueItems.parse("{not json"));
    }

    @Test
    public void importingTwiceAddsNothingTheSecondTime() throws JSONException {
        Map<String, String> legacy = QueueItems.parse("[" + item("op-1", "PENDING") + "," + item("op-2", "PENDING") + "]");
        Set<String> present = new HashSet<>(Set.of("op-1"));
        List<Map.Entry<String, String>> first = QueueItems.missing(legacy, present);
        assertEquals(1, first.size());
        assertEquals("op-2", first.get(0).getKey());
        present.add("op-2");
        assertTrue(QueueItems.missing(legacy, present).isEmpty());
        assertTrue(QueueItems.covers(present, legacy));
        assertFalse(QueueItems.covers(Set.of("op-1"), legacy));
    }
}
