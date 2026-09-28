#!/usr/bin/env bash
# Start a headless Android emulator on a CI runner and wait until it has booted.
# Uses only the SDK preinstalled on the runner (no third-party actions).
#   ANDROID_HOME must be set; KVM must be accessible.
set -euo pipefail
API="${SEOS_EMULATOR_API:-34}"
IMAGE="system-images;android-${API};google_apis;x86_64"
# avdmanager and the emulator must agree on where AVDs live; newer cmdline-tools
# can default to an XDG path the emulator does not read ("Unknown AVD name").
export ANDROID_SDK_ROOT="$ANDROID_HOME"
export ANDROID_AVD_HOME="${ANDROID_AVD_HOME:-$HOME/.android/avd}"
mkdir -p "$ANDROID_AVD_HOME"
SDKMANAGER="$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager"
AVDMANAGER="$ANDROID_HOME/cmdline-tools/latest/bin/avdmanager"

yes | "$SDKMANAGER" --licenses >/dev/null || true
"$SDKMANAGER" --install "platform-tools" "emulator" "$IMAGE" >/dev/null
echo no | "$AVDMANAGER" create avd --force --name seos-ci --package "$IMAGE" --device pixel_6 >/dev/null

LOG="${RUNNER_TEMP:-/tmp}/emulator.log"
fail() {
  echo "::error::$1"
  echo "--- emulator.log (tail) ---"; tail -80 "$LOG" 2>/dev/null || true
  exit 1
}
echo "kvm: $(ls -l /dev/kvm 2>&1)"
echo "avds in $ANDROID_AVD_HOME: $("$ANDROID_HOME/emulator/emulator" -list-avds 2>&1 | tr '\n' ' ')"
"$ANDROID_HOME/emulator/emulator" -accel-check || true

nohup "$ANDROID_HOME/emulator/emulator" -avd seos-ci -no-window -no-audio -no-boot-anim -no-snapshot \
  -no-metrics -gpu swiftshader_indirect -camera-back none -memory 3072 -netdelay none -netspeed full \
  > "$LOG" 2>&1 &
EMU_PID=$!

ADB="$ANDROID_HOME/platform-tools/adb"
"$ADB" start-server >/dev/null
# Every wait is bounded and watches the emulator process, so a crash fails the
# step at once with the emulator's own log instead of hanging until the job limit.
booted=""
for _ in $(seq 1 180); do
  kill -0 "$EMU_PID" 2>/dev/null || fail "emulator process exited before boot"
  if [ "$(timeout 10 "$ADB" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = "1" ]; then
    booted=1
    break
  fi
  sleep 3
done
[ -n "$booted" ] || fail "emulator did not finish booting within 9 minutes"
# Deterministic UI tests: no animations, stay awake.
"$ADB" shell settings put global window_animation_scale 0
"$ADB" shell settings put global transition_animation_scale 0
"$ADB" shell settings put global animator_duration_scale 0
"$ADB" shell svc power stayon true
"$ADB" shell input keyevent 82 || true
echo "emulator ready: API $API"
