#!/usr/bin/env python3
"""Device check of the install/update trust path, with real `adb install`.

    python mobile/scripts/install_upgrade_check.py OLD.apk NEW.apk FOREIGN.apk

OLD and NEW are the same app signed with the same key, NEW with a higher versionCode;
FOREIGN is NEW re-signed with another key. Proves on a real Android system:
clean install → the app starts offline → data survives an upgrade → a downgrade is
refused → an update signed with a different key is refused → uninstall + clean
install starts with no data.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

PACKAGE = "io.github.misha1302.seos"
ADB = os.path.join(os.environ.get("ANDROID_HOME", ""), "platform-tools", "adb") if os.environ.get("ANDROID_HOME") else "adb"


def adb(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run([ADB, *args], capture_output=True, text=True, timeout=240)
    if check and result.returncode != 0:
        raise AssertionError(f"adb {' '.join(args)} failed: {result.stdout}{result.stderr}")
    return result


def version_code() -> int:
    out = adb("shell", "dumpsys", "package", PACKAGE).stdout
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("versionCode="):
            return int(line.split("=", 1)[1].split()[0])
    raise AssertionError("package not installed")


def marker() -> str:
    return adb("shell", "run-as", PACKAGE, "cat", "files/seos-ci-marker", check=False).stdout.strip()


def start_app_offline() -> None:
    adb("shell", "svc", "wifi", "disable", check=False)
    adb("shell", "svc", "data", "disable", check=False)
    try:
        adb("shell", "monkey", "-p", PACKAGE, "-c", "android.intent.category.LAUNCHER", "1")
        time.sleep(8)
        pid = adb("shell", "pidof", PACKAGE, check=False).stdout.strip()
        assert pid, "the app is not running after an offline start"
        crashes = adb("logcat", "-d", "-b", "crash", check=False).stdout
        assert PACKAGE not in crashes, f"crash on offline start:\n{crashes[-2000:]}"
    finally:
        adb("shell", "svc", "wifi", "enable", check=False)
        adb("shell", "svc", "data", "enable", check=False)
        adb("shell", "am", "force-stop", PACKAGE, check=False)


def main(old: str, new: str, foreign: str) -> int:
    adb("uninstall", PACKAGE, check=False)
    adb("logcat", "-c", "-b", "crash", check=False)

    adb("install", old)
    old_code = version_code()
    start_app_offline()
    adb("shell", "run-as", PACKAGE, "sh", "-c", "mkdir -p files && echo kept > files/seos-ci-marker")
    assert marker() == "kept"

    adb("install", "-r", new)
    new_code = version_code()
    assert new_code > old_code, (old_code, new_code)
    assert marker() == "kept", "app data was lost on upgrade"
    start_app_offline()

    downgrade = adb("install", "-r", old, check=False)
    assert downgrade.returncode != 0 and "VERSION_DOWNGRADE" in downgrade.stdout + downgrade.stderr, downgrade
    assert version_code() == new_code

    foreign_update = adb("install", "-r", foreign, check=False)
    output = foreign_update.stdout + foreign_update.stderr
    assert foreign_update.returncode != 0 and ("UPDATE_INCOMPATIBLE" in output or "signatures do not match" in output), output
    assert version_code() == new_code and marker() == "kept"

    adb("uninstall", PACKAGE)
    adb("install", new)
    assert marker() == "", "a clean install must not see old data"
    print(f"install/upgrade/trust OK: {old_code} -> {new_code}; downgrade and foreign-key update refused")
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:4]))
