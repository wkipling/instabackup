#!/usr/bin/env python3
"""Offline regression test for wifi-connect.sh.

Runs the real wrapper against mock stock scripts and a fake `ip` command.
Covers the primary change_mode.sh path, the WPA3-capable fallback, and the
credential-injection checks.  No camera or root access needed.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def write(path: Path, text: str, mode: int = 0o755) -> None:
    path.write_text(text)
    os.chmod(path, mode)


def run_case(td: Path, *, primary_ok: bool) -> tuple[int, str, Path, Path]:
    sd = td / f"SD-{'primary' if primary_ok else 'fallback'}"
    sd.mkdir()
    (sd / "wifi-connect.sh").write_text(
        (HERE / "wifi-connect.sh").read_text())
    os.chmod(sd / "wifi-connect.sh", 0o755)
    write(sd / "wifi-sta.conf", "WIFI_SSID=TestHotspot\n"
                                "WIFI_PASSWORD=secret123\n"
                                "WIFI_COUNTRY=AU\n")

    marker = td / f"route-{'primary' if primary_ok else 'fallback'}"
    cmdlog = td / f"cmd-{'primary' if primary_ok else 'fallback'}.log"
    bin_dir = td / f"bin-{'primary' if primary_ok else 'fallback'}"
    bin_dir.mkdir()
    write(bin_dir / "iw", "#!/bin/sh\necho '\ttype AP'\nexit 0\n")
    write(bin_dir / "killall", f"#!/bin/sh\necho \"$*\" >> '{cmdlog}.kills'\nexit 0\n")
    write(bin_dir / "ifconfig", f"#!/bin/sh\necho \"$*\" >> '{cmdlog}.ifconfig'\nexit 0\n")
    write(bin_dir / "pidof", "#!/bin/sh\nexit 0\n")
    write(bin_dir / "wpa_cli", "#!/bin/sh\ncase \"$*\" in *status*) exit 1 ;; esac\nexit 0\n")
    for cmd in ("udhcpc", "hostapd", "hostapd_cli", "dnsmasq"):
        write(bin_dir / cmd, "#!/bin/sh\nexit 0\n")
    ip = bin_dir / "ip"
    write(ip, f"""#!/bin/sh
if [ "$1" = "route" ] && [ "$2" = "del" ]; then
    echo "$*" >> "{cmdlog}"
    exit 0
fi
if [ "$1" = "route" ]; then
    [ -f "{marker}" ] && echo "default via 192.168.42.1 dev wlan0"
    exit 0
fi
if [ "$1" = "-4" ]; then
    case "$*" in
        *wlan1*) echo "    inet 192.168.42.1/24 brd 192.168.42.255 scope global wlan1" ;;
        *) echo "    inet 192.168.42.170/24 brd 192.168.42.255 scope global wlan0" ;;
    esac
    exit 0
fi
if [ "$1" = "addr" ] && [ "$2" = "del" ]; then
    echo "$*" >> "{cmdlog}"
    exit 0
fi
exit 0
""")

    # Primary path: sta_start_fast + sta_connect succeeds.  Fallback path:
    # that pair fails and change_mode.sh supplies the route.
    change = td / f"change-{'primary' if primary_ok else 'fallback'}.sh"
    if primary_ok:
        write(change, "#!/bin/sh\nexit 1\n")
    else:
        write(change, f"#!/bin/sh\ntouch '{marker}'\nexit 0\n")

    fast = td / f"fast-{'primary' if primary_ok else 'fallback'}.sh"
    write(fast, "#!/bin/sh\nexit 0\n")
    connect = td / f"connect-{'primary' if primary_ok else 'fallback'}.sh"
    if primary_ok:
        write(connect, f"#!/bin/sh\ntouch '{marker}'\nexit 2\n")
    else:
        write(connect, "#!/bin/sh\nexit 1\n")

    env = dict(os.environ)
    env.update({
        "PATH": f"{bin_dir}:{env.get('PATH', '')}",
        "WIFI_CHANGE_MODE": str(change),
        "WIFI_START_FAST": str(fast),
        "WIFI_CONNECT": str(connect),
        "WIFI_CONF_OUT": str(td / "wifi.conf"),
        "WIFI_PREF_DIR": str(td / "pref"),
        "WIFI_WAIT": "3",
        "WIFI_SKIP_WAIT": "0",
    })
    p = subprocess.run(
        ["sh", str(sd / "wifi-connect.sh")], text=True,
        capture_output=True, env=env, cwd=sd)
    log = (sd / "bootup.log").read_text()
    return p.returncode, log, sd, cmdlog


def run_ap_fallback_case(td: Path) -> tuple[int, str, bool]:
    sd = td / "SD-ap-fallback"
    sd.mkdir()
    (sd / "wifi-connect.sh").write_text(
        (HERE / "wifi-connect.sh").read_text())
    os.chmod(sd / "wifi-connect.sh", 0o755)
    write(sd / "wifi-sta.conf", "WIFI_SSID=MissingNet\n"
                                "WIFI_PASSWORD=secret123\n")

    pref = td / "pref-ap-fallback"
    pref.mkdir()
    write(pref / "wifi.conf", "WIFI_MODE=ap\nAP_SSID=TestAP\n"
                                "AP_PASSWD=ap-secret\n")
    restored = td / "ap-restored"
    cmdlog = td / "cmd-ap-fallback.log"
    bin_dir = td / "bin-ap-fallback"
    bin_dir.mkdir()
    write(bin_dir / "iw", "#!/bin/sh\necho '\ttype AP'\nexit 0\n")
    write(bin_dir / "killall", f"#!/bin/sh\necho \"$*\" >> '{cmdlog}.kills'\nexit 0\n")
    write(bin_dir / "ifconfig", f"#!/bin/sh\necho \"$*\" >> '{cmdlog}.ifconfig'\nexit 0\n")
    write(bin_dir / "pidof", "#!/bin/sh\nexit 0\n")
    write(bin_dir / "wpa_cli", "#!/bin/sh\ncase \"$*\" in *status*) exit 1 ;; esac\nexit 0\n")
    for cmd in ("udhcpc", "hostapd", "hostapd_cli", "dnsmasq"):
        write(bin_dir / cmd, "#!/bin/sh\nexit 0\n")
    ip = bin_dir / "ip"
    write(ip, f"""#!/bin/sh
if [ "$1" = "route" ] && [ "$2" = "del" ]; then
    echo "$*" >> "{cmdlog}"
    exit 0
fi
if [ "$1" = "route" ]; then
    exit 0
fi
if [ "$1" = "-4" ]; then
    echo "    inet 192.168.42.1/24 scope global wlan1"
    exit 0
fi
exit 0
""")

    change = td / "change-ap-fallback.sh"
    write(change, f"""#!/bin/sh
mode=$(sed -n 's/^WIFI_MODE=//p' "$WIFI_CONF_OUT" 2>/dev/null)
if [ "$mode" = "ap" ]; then
    touch "{restored}"
    exit 0
fi
exit 1
""")
    fast = td / "fast-ap-fallback.sh"
    write(fast, "#!/bin/sh\nexit 1\n")
    connect = td / "connect-ap-fallback.sh"
    write(connect, "#!/bin/sh\nexit 1\n")

    env = dict(os.environ)
    env.update({
        "PATH": f"{bin_dir}:{env.get('PATH', '')}",
        "WIFI_CHANGE_MODE": str(change),
        "WIFI_START_FAST": str(fast),
        "WIFI_CONNECT": str(connect),
        "WIFI_CONF_OUT": str(td / "wifi-ap-fallback.conf"),
        "WIFI_PREF_DIR": str(pref),
        "WIFI_WAIT": "1",
        "WIFI_FALLBACK_AP": "1",
    })
    p = subprocess.run(
        ["sh", str(sd / "wifi-connect.sh")], text=True,
        capture_output=True, env=env, cwd=sd)
    return p.returncode, (sd / "bootup.log").read_text(), restored.exists()


def run_missing_ssid_case(td: Path, *, reconnect: bool) -> tuple[int, str, bool, bool]:
    """sta_connect.sh exit 11 means the saved SSID is not in range.

    At boot that must restore the camera's own AP and exit 3.  A watchdog
    reconnect must keep retrying station mode instead, so a brief hotspot
    outage cannot take the camera off the network for the rest of the boot.
    """
    tag = "reconnect" if reconnect else "boot"
    sd = td / f"SD-missing-{tag}"
    sd.mkdir()
    (sd / "wifi-connect.sh").write_text(
        (HERE / "wifi-connect.sh").read_text())
    os.chmod(sd / "wifi-connect.sh", 0o755)
    write(sd / "wifi-sta.conf", "WIFI_SSID=MissingNet\n"
                                "WIFI_PASSWORD=secret123\n")

    pref = td / f"pref-missing-{tag}"
    pref.mkdir()
    write(pref / "wifi.conf", "WIFI_MODE=ap\nAP_SSID=TestAP\n"
                                "AP_PASSWD=ap-secret\n")
    restored = td / f"ap-restored-{tag}"
    flag = td / f"ap-flag-{tag}"
    cmdlog = td / f"cmd-missing-{tag}.log"
    bin_dir = td / f"bin-missing-{tag}"
    bin_dir.mkdir()
    write(bin_dir / "iw", "#!/bin/sh\necho '\ttype AP'\nexit 0\n")
    write(bin_dir / "killall", "#!/bin/sh\nexit 0\n")
    write(bin_dir / "ifconfig", "#!/bin/sh\nexit 0\n")
    write(bin_dir / "pidof", "#!/bin/sh\nexit 0\n")
    write(bin_dir / "wpa_cli", "#!/bin/sh\nexit 0\n")
    for cmd in ("udhcpc", "hostapd", "hostapd_cli", "dnsmasq"):
        write(bin_dir / cmd, "#!/bin/sh\nexit 0\n")
    ip = bin_dir / "ip"
    write(ip, f"""#!/bin/sh
if [ "$1" = "route" ] && [ "$2" = "del" ]; then
    exit 0
fi
if [ "$1" = "route" ]; then
    exit 0
fi
if [ "$1" = "-4" ]; then
    echo "    inet 192.168.42.1/24 scope global wlan1"
    exit 0
fi
if [ "$1" = "addr" ] && [ "$2" = "del" ]; then
    exit 0
fi
exit 0
""")

    change = td / f"change-missing-{tag}.sh"
    write(change, f"""#!/bin/sh
mode=$(sed -n 's/^WIFI_MODE=//p' "$WIFI_CONF_OUT" 2>/dev/null)
if [ "$mode" = "ap" ]; then
    touch '{restored}'
    exit 0
fi
exit 1
""")
    fast = td / f"fast-missing-{tag}.sh"
    write(fast, "#!/bin/sh\nexit 0\n")
    connect = td / f"connect-missing-{tag}.sh"
    write(connect, "#!/bin/sh\nexit 11\n")

    env = dict(os.environ)
    env.update({
        "PATH": f"{bin_dir}:{env.get('PATH', '')}",
        "WIFI_CHANGE_MODE": str(change),
        "WIFI_START_FAST": str(fast),
        "WIFI_CONNECT": str(connect),
        "WIFI_CONF_OUT": str(td / f"wifi-missing-{tag}.conf"),
        "WIFI_PREF_DIR": str(pref),
        "WIFI_WAIT": "1",
        "WIFI_AP_FALLBACK_FLAG": str(flag),
    })
    if reconnect:
        env["WIFI_RECONNECT"] = "1"
    p = subprocess.run(
        ["sh", str(sd / "wifi-connect.sh")], text=True,
        capture_output=True, env=env, cwd=sd)
    return (p.returncode, (sd / "bootup.log").read_text(),
            restored.exists(), flag.exists())


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="wifi-connect-") as td_s:
        td = Path(td_s)

        for name, primary_ok, want_fallback in (
                ("primary", True, False), ("fallback", False, True)):
            case = td / f"case-{name}"
            case.mkdir()
            rc, log, sd, cmdlog = run_case(case, primary_ok=primary_ok)
            print(f"--- {name}: rc={rc}")
            print(log, end="")
            if rc != 0:
                print(f"FAIL: {name} case returned {rc}", file=sys.stderr)
                return 1
            if "station mode ready" not in log:
                print(f"FAIL: {name} case did not report a route", file=sys.stderr)
                return 1
            if "secret123" in log:
                print(f"FAIL: {name} case leaked the password", file=sys.stderr)
                return 1
            if want_fallback and "trying change_mode.sh" not in log:
                print("FAIL: change_mode fallback was not used", file=sys.stderr)
                return 1
            if not want_fallback and "trying change_mode.sh" in log:
                print("FAIL: change_mode fallback was used after a good join",
                      file=sys.stderr)
                return 1
            cmds = cmdlog.read_text() if cmdlog.exists() else ""
            if "addr del 192.168.42.1/24 dev wlan1" not in cmds:
                print("FAIL: AP address was not removed from wlan1", file=sys.stderr)
                return 1
            if "dev wlan0" in cmds:
                print("FAIL: station interface wlan0 was modified by AP cleanup",
                      file=sys.stderr)
                return 1

        # If the join fails, restore the original AP profile so the camera
        # does not become unreachable.
        rc, log, restored = run_ap_fallback_case(td)
        print(f"--- ap-fallback: rc={rc}")
        print(log, end="")
        if rc == 0:
            print("FAIL: failed join returned success", file=sys.stderr)
            return 1
        if "restoring the camera's own AP" not in log or not restored:
            print("FAIL: AP fallback was not started", file=sys.stderr)
            return 1
        if "AP restore rc=0" not in log:
            print("FAIL: AP restore did not succeed", file=sys.stderr)
            return 1

        # A missing SSID at boot must put the camera back into its normal AP
        # (rc 3) and leave a flag that keeps the wi-fi watchdog idle.
        rc, log, restored, flag = run_missing_ssid_case(td, reconnect=False)
        print(f"--- missing-ssid-boot: rc={rc}")
        print(log, end="")
        if rc != 3:
            print(f"FAIL: missing-SSID boot returned {rc}, want 3",
                  file=sys.stderr)
            return 1
        if "returning the camera to its normal AP" not in log:
            print("FAIL: missing-SSID boot did not log the fallback",
                  file=sys.stderr)
            return 1
        if not restored or not flag:
            print("FAIL: missing-SSID boot did not restore the stock AP",
                  file=sys.stderr)
            return 1
        if "secret123" in log:
            print("FAIL: missing-SSID case leaked the password", file=sys.stderr)
            return 1

        # The watchdog must not use that fallback: a temporary hotspot
        # outage has to keep retrying station mode.
        rc, log, restored, flag = run_missing_ssid_case(td, reconnect=True)
        print(f"--- missing-ssid-reconnect: rc={rc}")
        print(log, end="")
        if rc == 0 or restored or flag:
            print("FAIL: watchdog reconnect restored AP mode", file=sys.stderr)
            return 1
        if "sta_connect.sh rc=11" not in log:
            print("FAIL: reconnect did not report the stock exit 11",
                  file=sys.stderr)
            return 1

        # Reject a credential the stock wpa_supplicant generator cannot escape.
        bad = td / "bad"
        bad.mkdir()
        (bad / "wifi-sta.conf").write_text(
            'WIFI_SSID=Bad"SSID\nWIFI_PASSWORD=secret123\n')
        (bad / "wifi-connect.sh").write_text(
            (HERE / "wifi-connect.sh").read_text())
        os.chmod(bad / "wifi-connect.sh", 0o755)
        p = subprocess.run(["sh", str(bad / "wifi-connect.sh")],
                           text=True, capture_output=True, cwd=bad)
        if p.returncode == 0:
            print("FAIL: quoted SSID was accepted", file=sys.stderr)
            return 1

    print("PASS: wifi-connect.sh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
