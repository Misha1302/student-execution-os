#!/usr/bin/env bash
# Start a headless Android emulator on a CI runner and wait until it has booted.
# Uses only the SDK preinstalled on the runner (no third-party actions).
#   ANDROID_HOME must be set; KVM must be accessible.
set -euo pipefail
API="${SEOS_EMULATOR_API:-34}"
IMAGE="system-images;android-${API};google_apis;x86_64"
SDKMANAGER="$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager"
AVDMANAGER="$ANDROID_HOME/cmdline-tools/latest/bin/avdmanager"

yes | "$SDKMANAGER" --licenses >/dev/null || true
"$SDKMANAGER" --install "platform-tools" "emulator" "$IMAGE" >/dev/null
echo no | "$AVDMANAGER" create avd --force --name seos-ci --package "$IMAGE" --device pixel_6 >/dev/null

nohup "$ANDROID_HOME/emulator/emulator" -avd seos-ci -no-window -no-audio -no-boot-anim -no-snapshot \
  -gpu swiftshader_indirect -camera-back none -memory 3072 -netdelay none -netspeed full \
  > "${RUNNER_TEMP:-/tmp}/emulator.log" 2>&1 &

ADB="$ANDROID_HOME/platform-tools/adb"
"$ADB" wait-for-device
for _ in $(seq 1 240); do
  if [ "$("$ADB" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = "1" ]; then
    break
  fi
  sleep 2
done
test "$("$ADB" shell getprop sys.boot_completed | tr -d '\r')" = "1" || { tail -50 "${RUNNER_TEMP:-/tmp}/emulator.log"; exit 1; }
# Deterministic UI tests: no animations, stay awake.
"$ADB" shell settings put global window_animation_scale 0
"$ADB" shell settings put global transition_animation_scale 0
"$ADB" shell settings put global animator_duration_scale 0
"$ADB" shell svc power stayon true
"$ADB" shell input keyevent 82 || true
echo "emulator ready: API $API"
