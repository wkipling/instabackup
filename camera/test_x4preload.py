#!/usr/bin/env python3
"""Host-side test for the X4 preload constructor.

Builds the x86_64 test variant (which skips the instaAIP wait), then checks:
  - no /tmp/SD0/bootup.sh means no launch and no lock;
  - a visible bootup.sh is launched through /bin/sh despite mode 0644;
  - the one-shot lock prevents a second launch;
  - the lock is removed if the script disappears before the next boot.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BUILD = os.path.join(ROOT, "camera/build/libcloudbootstrap-host.so")
SD = "/tmp/SD0"
LOCK = "/tmp/.cloudbootstrap.started"
MARK = "/tmp/x4preload-test.log"


def clean():
    shutil.rmtree(SD, ignore_errors=True)
    for p in (LOCK, MARK):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass


def run_preloaded():
    env = dict(os.environ)
    env["LD_PRELOAD"] = BUILD
    subprocess.run(["/bin/true"], env=env, check=True, timeout=10)


def wait_for(path: str, timeout: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(path):
            return True
        time.sleep(0.05)
    return False


def main() -> int:
    subprocess.run([os.path.join(HERE, "build_x4preload.sh")],
                   check=True, stdout=subprocess.DEVNULL)
    clean()

    run_preloaded()
    assert not os.path.exists(LOCK), "lock created without bootup.sh"
    print("no script -> no action: OK")

    os.makedirs(SD, exist_ok=True)
    with open(os.path.join(SD, "bootup.sh"), "w") as fh:
        fh.write("#!/bin/sh\n")
        fh.write('echo "ran" > ' + MARK + "\n")
    os.chmod(os.path.join(SD, "bootup.sh"), 0o644)
    run_preloaded()
    assert wait_for(MARK), "bootup.sh was not launched"
    assert wait_for(LOCK), "lock was not created"
    print("visible script -> launched via /bin/sh: OK")

    os.remove(MARK)
    run_preloaded()
    time.sleep(0.5)
    assert not os.path.exists(MARK), "lock did not prevent a second launch"
    print("one-shot lock: OK")

    os.remove(LOCK)
    os.remove(os.path.join(SD, "bootup.sh"))
    run_preloaded()
    time.sleep(0.3)
    assert not os.path.exists(LOCK), "lock was created without bootup.sh"
    print("script removed -> no action: OK")

    clean()
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
