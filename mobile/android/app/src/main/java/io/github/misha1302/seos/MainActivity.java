package io.github.misha1302.seos;

import android.os.Bundle;
import com.getcapacitor.BridgeActivity;
import io.github.misha1302.seos.alarm.SeosNativePlugin;
import io.github.misha1302.seos.updates.SeosUpdatePlugin;

public class MainActivity extends BridgeActivity {
    @Override
    public void onCreate(Bundle savedInstanceState) {
        registerPlugin(SeosNativePlugin.class);
        registerPlugin(SeosUpdatePlugin.class);
        super.onCreate(savedInstanceState);
    }
}
