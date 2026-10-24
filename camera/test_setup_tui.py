#!/usr/bin/env python3
"""Offline tests for setup_tui.py's pure planning helpers.

No card, terminal, network or camera is needed.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import setup_tui as t  # noqa: E402


class BuildArgv(unittest.TestCase):
    def test_tailscale_plan(self):
        plan = t.Plan(sd="/run/media/me/disk", mode="tailscale",
                      target="100.64.0.5", token_file="/tmp/tok",
                      tailscale_dir="/opt/ts", delete_after=True)
        argv = t.build_install_argv(plan, wifi_conf="/tmp/wifi.local",
                                    token_file=plan.token_file)
        self.assertEqual(argv[0], "sh")
        self.assertTrue(argv[1].endswith("install_to_sd.sh"))
        self.assertEqual(argv[2], "/run/media/me/disk")
        self.assertEqual(argv[3], "-")
        self.assertIn("--mode", argv)
        self.assertEqual(argv[argv.index("--mode") + 1], "tailscale")
        self.assertEqual(argv[argv.index("--target") + 1], "100.64.0.5")
        self.assertEqual(argv[argv.index("--tailscale-dir") + 1], "/opt/ts")
        self.assertEqual(argv[argv.index("--wifi-conf") + 1], "/tmp/wifi.local")
        self.assertEqual(argv[argv.index("--upload-token-file") + 1], "/tmp/tok")
        self.assertIn("--x4-air", argv)

    def test_lan_plan_uses_server_ip(self):
        plan = t.Plan(sd="/media/card", mode="lan", server_ip="192.168.1.10")
        argv = t.build_install_argv(plan)
        self.assertEqual(argv[3], "192.168.1.10")
        self.assertNotIn("--target", argv)
        self.assertNotIn("--wifi-conf", argv)

    def test_no_wifi(self):
        plan = t.Plan(sd="/media/card", mode="lan", server_ip="10.0.0.2",
                      no_wifi=True)
        self.assertIn("--no-wifi", t.build_install_argv(plan))

    def test_password_never_in_argv(self):
        plan = t.Plan(sd="/media/card", mode="tailscale", target="100.64.0.5",
                      ssid="Hotel", password="hunter2")
        argv = t.build_install_argv(plan, wifi_conf="/tmp/wifi.local")
        self.assertNotIn("hunter2", " ".join(argv))
        self.assertNotIn("--wifi-password", argv)

    def test_authkey_file_is_preferred(self):
        plan = t.Plan(sd="/media/card", mode="tailscale",
                      target="100.64.0.5", authkey="tskey-auth-test")
        argv = t.build_install_argv(plan, authkey_file="/tmp/ak")
        self.assertEqual(argv[argv.index("--authkey-file") + 1], "/tmp/ak")
        self.assertNotIn("--authkey", argv)
        self.assertNotIn("tskey-auth-test", " ".join(argv))

    def test_authkey_argv_is_legacy_fallback(self):
        plan = t.Plan(sd="/media/card", mode="tailscale",
                      target="100.64.0.5", authkey="tskey-auth-test")
        argv = t.build_install_argv(plan)
        self.assertEqual(argv[argv.index("--authkey") + 1],
                         "tskey-auth-test")

    def test_missing_target_rejected(self):
        with self.assertRaises(ValueError):
            t.build_install_argv(t.Plan(sd="/media/card", mode="tailscale"))

    def test_missing_lan_ip_rejected(self):
        with self.assertRaises(ValueError):
            t.build_install_argv(t.Plan(sd="/media/card", mode="lan"))

    def test_missing_card_rejected(self):
        with self.assertRaises(ValueError):
            t.build_install_argv(
                t.Plan(mode="lan", server_ip="10.0.0.2"))


class ConfEditing(unittest.TestCase):
    SAMPLE = """# receiver settings
BACKUP_MODE=lan
SERVER_IP=192.168.1.2
#TAILNET_TARGET=
START_SD_UPLOAD=0
START_TELNETD=1
# keep this comment
SD_UPLOAD_DELETE=1
"""

    def test_updates_and_adds(self):
        values = {
            "BACKUP_MODE": "tailscale",
            "TAILNET_TARGET": "100.64.0.5",
            "START_SD_UPLOAD": "1",
            "START_TELNETD": "0",
            "SD_UPLOAD_INTERVAL": "60",
        }
        out = t.update_conf_text(self.SAMPLE, values)
        self.assertIn("BACKUP_MODE=tailscale", out)
        self.assertIn("TAILNET_TARGET=100.64.0.5", out)
        self.assertIn("START_SD_UPLOAD=1", out)
        self.assertIn("START_TELNETD=0", out)
        self.assertIn("SD_UPLOAD_INTERVAL=60", out)
        self.assertNotIn("#TAILNET_TARGET=", out)
        self.assertIn("# keep this comment", out)

    def test_preserves_unrelated_keys_and_comments(self):
        out = t.update_conf_text(self.SAMPLE, {"START_TELNETD": "0"})
        self.assertIn("SERVER_IP=192.168.1.2", out)
        self.assertIn("# receiver settings", out)
        self.assertIn("SD_UPLOAD_DELETE=1", out)

    def test_deduplicates(self):
        text = "START_SD_UPLOAD=0\nSTART_SD_UPLOAD=1\n"
        out = t.update_conf_text(text, {"START_SD_UPLOAD": "1"})
        self.assertEqual(out.count("START_SD_UPLOAD="), 1)
        self.assertIn("START_SD_UPLOAD=1", out)

    def test_always_ends_with_newline(self):
        self.assertTrue(
            t.update_conf_text("A=1", {"B": "2"}).endswith("\n"))

    def test_ignores_prose_starting_with_hash(self):
        text = "# 1 = delete the file\nSTART_SD_UPLOAD=0\n"
        out = t.update_conf_text(text, {"START_SD_UPLOAD": "1"})
        self.assertIn("# 1 = delete the file", out)


class RuntimeValues(unittest.TestCase):
    def test_tailscale_clears_lan_ip(self):
        plan = t.Plan(mode="tailscale", target="100.64.0.5", server_ip="10.0.0.2")
        values = t.conf_values(plan, has_token=True)
        self.assertEqual(values["BACKUP_MODE"], "tailscale")
        self.assertEqual(values["TAILNET_TARGET"], "100.64.0.5")
        self.assertEqual(values["SERVER_IP"], "")
        self.assertEqual(values["DISABLE_CLOUD_REDIRECT"], "1")
        self.assertEqual(values["START_CLOUD_UPLOAD"], "0")
        self.assertEqual(values["START_SD_UPLOAD"], "1")
        self.assertEqual(values["START_TELNETD"], "0")

    def test_lan_clears_tailnet(self):
        plan = t.Plan(mode="lan", target="100.64.0.5", server_ip="10.0.0.2")
        values = t.conf_values(plan, has_token=False)
        self.assertEqual(values["SERVER_IP"], "10.0.0.2")
        self.assertEqual(values["TAILNET_TARGET"], "")

    def test_authkey_absent_does_not_wipe_saved_key(self):
        plan = t.Plan(mode="tailscale", target="100.64.0.5")
        self.assertNotIn("TAILSCALE_AUTHKEY",
                         t.conf_values(plan, has_token=False))

    def test_authkey_supplied_is_stored(self):
        plan = t.Plan(mode="tailscale", target="100.64.0.5",
                      authkey="tskey-auth-test")
        self.assertEqual(t.conf_values(plan, has_token=False)
                         ["TAILSCALE_AUTHKEY"], "tskey-auth-test")


class Wifi(unittest.TestCase):
    def test_render(self):
        plan = t.Plan(ssid="Hotel", password="pw", country="AU")
        text = t.render_wifi_conf(plan)
        self.assertIn("WIFI_SSID=Hotel\n", text)
        self.assertIn("WIFI_PASSWORD=pw\n", text)
        self.assertIn("WIFI_COUNTRY=AU\n", text)

    def test_validation(self):
        self.assertEqual(t.validate_wifi("ok", "pw"), "")
        self.assertTrue(t.validate_wifi("", "pw"))
        self.assertTrue(t.validate_wifi('bad"ssid', "pw"))
        self.assertTrue(t.validate_wifi("ssid", "back\\slash"))
        self.assertTrue(t.validate_wifi("new\nline", "pw"))

    def test_parse_nmcli_escaped_and_dedup(self):
        text = (
            "Cafe\\:Guest:44:WPA2\n"
            "Cafe\\:Guest:71:WPA2\n"
            "OpenNet:30:--\n"
            ":12:WPA2\n"
        )
        nets = t.parse_nmcli_scan(text)
        by_ssid = {s: (sig, sec) for s, sig, sec in nets}
        self.assertEqual(by_ssid["Cafe:Guest"], (71, "WPA2"))
        self.assertEqual(by_ssid["OpenNet"], (30, ""))
        self.assertNotIn("", by_ssid)
        self.assertEqual(nets[0][0], "Cafe:Guest")  # strongest first


class CardDetection(unittest.TestCase):
    def test_detects_only_removable_roots(self):
        with tempfile.TemporaryDirectory() as td:
            card = Path(td) / "disk"
            (card / "DCIM").mkdir(parents=True)
            other = Path(td) / "rootfs"
            other.mkdir()
            mounts = Path(td) / "mounts"
            mounts.write_text(
                "/dev/mmcblk0p1 %s exfat rw 0 0\n"
                "/dev/sda1 / ext4 rw 0 0\n"
                "/dev/loop0 /snap/core squashfs ro 0 0\n" % card)
            found = t.detect_cards(str(mounts), roots=(td,))
            self.assertEqual(found, [str(card)])


class Checklist(unittest.TestCase):
    def test_mentions_dashboard_and_fallback(self):
        plan = t.Plan(sd="/run/media/me/disk", mode="tailscale",
                      target="100.64.0.5", port=8844)
        text = t._checklist(plan)
        self.assertIn("http://100.64.0.5:8844/", text)
        self.assertIn("15 seconds", text)

    def test_does_not_leak_password(self):
        plan = t.Plan(sd="/x", mode="lan", server_ip="10.0.0.2",
                      ssid="S", password="hunter2")
        self.assertNotIn("hunter2", t._summary(plan))


if __name__ == "__main__":
    unittest.main(verbosity=2)
