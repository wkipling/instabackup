#!/usr/bin/env python3
"""Regression test for install_to_sd.sh --x4-air.

Builds a throwaway SD-card directory, runs the real installer, and checks the
X4 Air-specific behaviour plus verify_sd.sh.  No camera or root access needed.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, text=True, capture_output=True, **kw)


def main() -> int:
    if not (HERE / "install_to_sd.sh").is_file():
        print("install_to_sd.sh not found", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory(prefix="x4air-install-") as td:
        sd = Path(td) / "SD"
        sd.mkdir()

        p = run([str(HERE / "install_to_sd.sh"), str(sd), "192.168.1.10",
                 "--x4-air", "--upload-token", "test-token-xyz"])
        print(p.stdout, end="")
        if p.returncode != 0:
            print(p.stderr, end="", file=sys.stderr)
            return 1
        if "x4_bootstrap.py" in p.stdout:
            print("FAIL: X4 Air install mentions the X4 bootstrap", file=sys.stderr)
            return 1

        for name in ("bootup.sh", "cloud-upload-watch.sh", "cloudkick",
                     "tailrelay", "wifi-connect.sh", "insupload", "ca.crt",
                     "ca.hash", "cloud-backup.conf"):
            if not (sd / name).is_file():
                print(f"FAIL: missing {name}", file=sys.stderr)
                return 1

        for name in ("NO_HIBER", "DCIM"):
            if not (sd / name).exists():
                print(f"FAIL: missing {name}", file=sys.stderr)
                return 1

        conf = (sd / "cloud-backup.conf").read_text()
        for line in ("BACKUP_MODE=lan", "START_CLOUD_UPLOAD=1",
                     "START_SD_UPLOAD=1", "SD_UPLOAD_PORT=8844",
                     "RESTART_AIP=1"):
            if line not in conf:
                print(f"FAIL: {line} missing from cloud-backup.conf", file=sys.stderr)
                return 1
        token = sd / "sd-upload.token"
        if not token.is_file() or token.read_text().strip() != "test-token-xyz":
            print("FAIL: sd-upload.token missing or wrong", file=sys.stderr)
            return 1
        if "TAILNET_TARGET=100." in conf:
            print("FAIL: LAN install wrote a tailnet target", file=sys.stderr)
            return 1

        v = run([str(HERE / "verify_sd.sh"), str(sd)])
        print(v.stdout, end="")
        if v.returncode != 0 or "RESULT: ready (0 warning(s))" not in v.stdout:
            print(v.stderr, end="", file=sys.stderr)
            print("FAIL: verify_sd.sh did not report a clean card", file=sys.stderr)
            return 1

        # Re-run with station-mode Wi-Fi credentials.
        p = run([str(HERE / "install_to_sd.sh"), str(sd), "192.168.1.10",
                 "--x4-air", "--wifi-ssid", "TestHotspot",
                 "--wifi-password", "secret123"])
        print(p.stdout, end="")
        if p.returncode != 0:
            print(p.stderr, end="", file=sys.stderr)
            return 1
        wifi = (sd / "wifi-sta.conf").read_text()
        for line in ("WIFI_SSID=TestHotspot", "WIFI_PASSWORD=secret123"):
            if line not in wifi:
                print(f"FAIL: {line} missing from wifi-sta.conf", file=sys.stderr)
                return 1
        v = run([str(HERE / "verify_sd.sh"), str(sd)])
        print(v.stdout, end="")
        if v.returncode != 0 or "WIFI_SSID=TestHotspot" not in v.stdout:
            print(v.stderr, end="", file=sys.stderr)
            print("FAIL: verify_sd.sh did not accept station-mode Wi-Fi", file=sys.stderr)
            return 1

        # --wifi-conf is the no-shell-history path.  It must parse raw
        # WIFI_* lines, honour a country override, and never echo the secret.
        wifi_src = Path(td) / "wifi-sta.local"
        wifi_src.write_text(
            "WIFI_SSID=FileHotspot\n"
            "WIFI_PASSWORD=file-secret-123\n"
            "WIFI_COUNTRY=AU\n"
            "WIFI_BSSID=aa:bb:cc:dd:ee:ff\n"
        )
        p = run([str(HERE / "install_to_sd.sh"), str(sd), "192.168.1.10",
                 "--x4-air", "--wifi-conf", str(wifi_src)])
        print(p.stdout, end="")
        if p.returncode != 0:
            print(p.stderr, end="", file=sys.stderr)
            return 1
        if "file-secret-123" in p.stdout or "file-secret-123" in p.stderr:
            print("FAIL: installer echoed the Wi-Fi password", file=sys.stderr)
            return 1
        wifi = (sd / "wifi-sta.conf").read_text()
        for line in ("WIFI_SSID=FileHotspot", "WIFI_PASSWORD=file-secret-123",
                     "WIFI_COUNTRY=AU", "WIFI_BSSID=aa:bb:cc:dd:ee:ff"):
            if line not in wifi:
                print(f"FAIL: {line} missing from --wifi-conf output", file=sys.stderr)
                return 1

        # A quote would break the stock wpa_supplicant config generator.
        bad = Path(td) / "bad-wifi.conf"
        bad.write_text('WIFI_SSID=Bad"SSID\nWIFI_PASSWORD=secret123\n')
        p = run([str(HERE / "install_to_sd.sh"), str(sd), "192.168.1.10",
                 "--x4-air", "--wifi-conf", str(bad)])
        if p.returncode == 0:
            print("FAIL: installer accepted a quoted SSID", file=sys.stderr)
            return 1

        # A Tailscale auth key can stay out of argv as well.
        ak = Path(td) / "authkey"
        ak.write_text("tskey-auth-file-secret\n")
        fake_ts = Path(td) / "ts"
        fake_ts.mkdir()
        (fake_ts / "tailscaled").write_text("")
        (fake_ts / "tailscale").write_text("")
        p = run([str(HERE / "install_to_sd.sh"), str(sd), "-",
                 "--mode", "tailscale", "--target", "100.64.0.5",
                 "--x4-air", "--authkey-file", str(ak),
                 "--tailscale-dir", str(fake_ts)])
        print(p.stdout, end="")
        if p.returncode != 0:
            print(p.stderr, end="", file=sys.stderr)
            return 1
        if "tskey-auth-file-secret" in p.stdout or \
           "tskey-auth-file-secret" in p.stderr:
            print("FAIL: installer echoed the auth key", file=sys.stderr)
            return 1
        p = run([str(HERE / "install_to_sd.sh"), str(sd), "-",
                 "--mode", "tailscale", "--target", "100.64.0.5",
                 "--authkey", "a", "--authkey-file", str(ak)])
        if p.returncode == 0:
            print("FAIL: --authkey and --authkey-file were both accepted",
                  file=sys.stderr)
            return 1

    print("PASS: X4 Air SD install")
    return 0


if __name__ == "__main__":
    sys.exit(main())
