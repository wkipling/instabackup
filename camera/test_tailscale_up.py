#!/usr/bin/env python3
"""Behaviour tests for tailscale-up.sh with fake daemon/CLI binaries.

The real card bug: a valid saved node state was ignored and `tailscale up`
was run on every boot.  The 25 MB CLI has produced bad reads on this camera's
exFAT mount and segfaulted, and one crash loop then disabled Tailscale until
the next boot.  These tests pin the two paths:

  * saved state  -> start the daemon, never invoke the CLI
  * no state     -> invoke `tailscale up` with the auth key, then mark login

Run: python3 camera/test_tailscale_up.py
"""
import os
import shutil
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
PIDFILE = Path("/tmp/inscloud-tailscaled.pid")

FAKE_DAEMON = """#!/bin/sh
echo "DAEMON $*" >> "$FAKE_LOG"
sock=""
for a in "$@"; do
    case "$a" in
        --socket=*) sock="${a#--socket=}" ;;
    esac
done
[ -n "$sock" ] || exit 2
exec python3 -c '
import os, socket, sys, time
path = sys.argv[1]
try:
    os.unlink(path)
except OSError:
    pass
s = socket.socket(socket.AF_UNIX)
s.bind(path)
s.listen(4)
time.sleep(60)
' "$sock"
"""

FAKE_CLI = """#!/bin/sh
echo "CLI $*" >> "$FAKE_LOG"
exit 0
"""


class TailscaleUpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tsup-"))
        self.card = self.tmp / "card"
        (self.card / "tailscale").mkdir(parents=True)
        self.fakebin = self.tmp / "bin"
        self.fakebin.mkdir()
        self.log = self.tmp / "fake.log"
        self.log.write_text("")
        self.daemon_pid_file = self.tmp / "daemon.pid"

        # The host this test runs on may already have a real tailscaled.
        # Pretend pidof never finds one so the script uses its own PID file.
        pidof = self.fakebin / "pidof"
        pidof.write_text("#!/bin/sh\nexit 1\n")
        pidof.chmod(0o755)

        for name, body in (("tailscaled", FAKE_DAEMON), ("tailscale", FAKE_CLI)):
            path = self.card / "tailscale" / name
            path.write_text(body)
            path.chmod(0o755)

        shutil.copy(HERE / "tailscale-up.sh", self.card / "tailscale-up.sh")
        self.env = os.environ.copy()
        self.env["PATH"] = str(self.fakebin) + os.pathsep + self.env["PATH"]
        self.env["FAKE_LOG"] = str(self.log)
        self.env["FAKE_DAEMON_PID"] = str(self.daemon_pid_file)
        self.env.pop("TAILSCALE_STATE", None)
        self.env.pop("TAILSCALE_SOCKET", None)

    def tearDown(self):
        for path in (PIDFILE, self.daemon_pid_file):
            try:
                pid = int(path.read_text().strip())
            except (OSError, ValueError):
                continue
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        PIDFILE.unlink(missing_ok=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_conf(self, extra: str = "") -> None:
        (self.card / "cloud-backup.conf").write_text(
            "BACKUP_MODE=tailscale\n"
            "TAILSCALE_HOSTNAME=test-cam\n"
            "TAILSCALE_PROXY_PORT=18080\n"
            + extra)

    def _run(self) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["sh", str(self.card / "tailscale-up.sh")],
            cwd=self.card, env=self.env, capture_output=True, text=True,
            timeout=90)

    def test_saved_state_never_runs_the_cli(self):
        self._write_conf()
        (self.card / "tailscale" / "tailscaled.state").write_text("{}")
        PIDFILE.unlink(missing_ok=True)

        r = self._run()
        log = self.log.read_text()
        boot = (self.card / "bootup.log").read_text()

        self.assertEqual(r.returncode, 0, r.stderr + boot)
        self.assertIn("DAEMON", log)
        self.assertNotIn("CLI", log, "the CLI must not run with saved state")
        self.assertIn("saved node state", boot)
        self.assertTrue((self.card / "tailscale" / ".logged-in").exists())

    def test_first_login_uses_the_auth_key(self):
        self._write_conf("TAILSCALE_AUTHKEY=tskey-auth-test\n")
        (self.card / "tailscale" / "tailscaled.state").unlink(missing_ok=True)
        PIDFILE.unlink(missing_ok=True)

        r = self._run()
        log = self.log.read_text()
        boot = (self.card / "bootup.log").read_text()

        self.assertEqual(r.returncode, 0, r.stderr + boot)
        self.assertIn("running tailscale up with auth key", boot)
        self.assertIn("CLI", log)
        self.assertTrue((self.card / "tailscale" / ".logged-in").exists())


if __name__ == "__main__":
    unittest.main()
