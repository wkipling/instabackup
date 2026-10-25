#!/usr/bin/env python3
"""Onboarding TUI: prepare an Insta360 X4 Air SD card for self-hosted backup.

Run this on the computer with the camera card mounted:

    python3 setup_tui.py

It writes a station-mode Wi-Fi profile, installs the boot hook and uploader
onto the card, sets the receiver address, and verifies the result.

The TUI uses only the Python standard library (curses).  The Wi-Fi password
is read without echo, written to a mode-0600 temporary file, passed to
install_to_sd.sh as --wifi-conf, and deleted afterwards.  It never appears in
argv, in the summary, or in a log.

The non-interactive form exists for scripts and tests:

    python3 setup_tui.py --sd /run/media/me/disk --non-interactive \
        --wifi-conf /tmp/wifi.conf --mode tailscale --target 100.64.0.5 \
        --token-file .sd-upload.token

Use --dry-run to print the plan without touching the card.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

try:
    import curses
except ImportError:  # pragma: no cover - non-Unix hosts
    curses = None  # type: ignore[assignment]

HERE = Path(__file__).resolve().parent
CONF_NAME = "cloud-backup.conf"
TOKEN_NAME = ".sd-upload.token"


# ---------------------------------------------------------------------------
# Plan and pure builders (unit-tested without a terminal)
# ---------------------------------------------------------------------------


@dataclass
class Plan:
    sd: str = ""
    ssid: str = ""
    password: str = ""
    country: str = ""
    wifi_conf: str = ""       # existing file, instead of ssid/password
    no_wifi: bool = False
    mode: str = "tailscale"   # tailscale | lan
    target: str = ""          # tailnet IP in tailscale mode
    server_ip: str = ""       # LAN IP in lan mode
    port: int = 8844
    token_file: str = ""      # host-side file copied to sd-upload.token
    authkey: str = ""         # first Tailscale login (kept off argv in TUI)
    reset_tailscale: bool = False  # drop the saved node state before install
    tailscale_dir: str = ""
    delete_after: bool = False
    interval: int = 60
    lan_url: str = ""         # optional second URL tried by the uploader

    def effective_server(self) -> str:
        return self.target if self.mode == "tailscale" else self.server_ip

    def upload_url(self) -> str:
        host = self.effective_server() or "<server>"
        return "http://%s:%d" % (host, self.port)


def build_install_argv(plan: Plan, wifi_conf: str = "",
                       token_file: str = "",
                       authkey_file: str = "") -> list[str]:
    """Build install_to_sd.sh's argv.

    Secrets are deliberately absent from argv: the Wi-Fi password is
    supplied through wifi_conf, the upload token through token_file, and the
    Tailscale auth key through authkey_file.  ``plan.authkey`` is a fallback
    for manual installer use and is never used by the TUI.
    """
    if plan.mode not in ("tailscale", "lan"):
        raise ValueError("mode must be tailscale or lan")
    if plan.mode == "tailscale" and not plan.target:
        raise ValueError("tailscale mode needs a tailnet target")
    if plan.mode == "lan" and not plan.server_ip:
        raise ValueError("lan mode needs a server IP")
    if not plan.sd:
        raise ValueError("no SD card mountpoint")

    argv = [
        "sh", str(HERE / "install_to_sd.sh"),
        plan.sd,
        plan.server_ip if plan.mode == "lan" else "-",
        "--mode", plan.mode,
    ]
    if plan.mode == "tailscale":
        argv += ["--target", plan.target]
        if plan.tailscale_dir:
            argv += ["--tailscale-dir", plan.tailscale_dir]
        if authkey_file:
            argv += ["--authkey-file", authkey_file]
        elif plan.authkey:
            argv += ["--authkey", plan.authkey]
    argv += ["--x4-air"]
    if wifi_conf:
        argv += ["--wifi-conf", wifi_conf]
    elif plan.no_wifi:
        argv += ["--no-wifi"]
    if token_file:
        argv += ["--upload-token-file", token_file]
    return argv


def conf_values(plan: Plan, *, has_token: bool) -> dict[str, str]:
    """Runtime keys the card's cloud-backup.conf must contain."""
    values = {
        "BACKUP_MODE": plan.mode,
        "SERVER_IP": plan.server_ip if plan.mode == "lan" else "",
        "TAILNET_TARGET": plan.target if plan.mode == "tailscale" else "",
        "DISABLE_CLOUD_REDIRECT": "1",
        "START_CLOUD_UPLOAD": "0",
        "START_SD_UPLOAD": "1",
        "SD_UPLOAD_PORT": str(plan.port),
        "SD_UPLOAD_INTERVAL": str(plan.interval),
        "SD_UPLOAD_DELETE": "1" if plan.delete_after else "0",
        "START_WIFI_WATCHDOG": "1",
        "START_TELNETD": "0",
    }
    if plan.lan_url:
        values["SD_UPLOAD_LAN_URL"] = plan.lan_url
    # Only replace the stored auth key when a new one was supplied; an
    # existing key (or a saved tailscaled.state) must survive a re-run.
    if plan.authkey:
        values["TAILSCALE_AUTHKEY"] = plan.authkey
    return values


KEY_RE = re.compile(r"^#?\s*([A-Z][A-Z0-9_]*)=(.*)$")


def update_conf_text(text: str, values: dict[str, str]) -> str:
    """Set keys in an existing conf, preserving comments and other keys.

    Matches active and single-commented keys, drops later duplicates, and
    appends keys that were not present at all.
    """
    out: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        m = KEY_RE.match(line)
        key = m.group(1) if m else ""
        if key and key in values:
            if key in seen:
                continue
            out.append("%s=%s" % (key, values[key]))
            seen.add(key)
        else:
            out.append(line)
    for key, value in values.items():
        if key not in seen:
            out.append("%s=%s" % (key, value))
    return "\n".join(out).rstrip("\n") + "\n"


def write_text(path: Path, text: str, mode: int = 0o644) -> None:
    """Write via a temporary file and rename, then flush the directory."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    try:
        os.chmod(tmp, mode)
    except OSError:
        pass
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Discovery helpers
# ---------------------------------------------------------------------------


def _unescape_mount(value: str) -> str:
    return (value.replace("\\040", " ").replace("\\011", "\t")
            .replace("\\012", "\n").replace("\\134", "\\"))


def detect_cards(mounts_path: str = "/proc/self/mounts",
                 roots: tuple[str, ...] | None = None) -> list[str]:
    """Return plausible camera-card mountpoints, best candidate first."""
    if roots is None:
        roots = ("/run/media", "/media", "/mnt")
    found: list[str] = []
    try:
        with open(mounts_path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        lines = []
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        mount = _unescape_mount(parts[1])
        fstype = parts[2].lower()
        if fstype not in ("vfat", "exfat", "msdos", "fuseblk", "ntfs",
                          "ntfs3", "ext4"):
            continue
        if not any(mount == root or mount.startswith(root + "/")
                   for root in roots):
            continue
        if mount in ("/mnt", "/media"):
            continue
        found.append(mount)
    # Prefer a mount that already looks like a camera card.
    def rank(path: str) -> tuple[int, int]:
        try:
            has_dcim = os.path.isdir(os.path.join(path, "DCIM"))
            has_conf = os.path.isfile(os.path.join(path, CONF_NAME))
        except OSError:
            has_dcim = has_conf = False
        return (0 if (has_dcim or has_conf) else 1, len(path))
    return sorted(dict.fromkeys(found), key=rank)


def detect_tailnet_ip() -> str:
    exe = shutil.which("tailscale")
    if not exe:
        return ""
    try:
        out = subprocess.run([exe, "ip", "-4"], capture_output=True,
                             text=True, timeout=4)
    except (OSError, subprocess.SubprocessError):
        return ""
    for line in out.stdout.splitlines():
        line = line.strip()
        if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", line):
            return line
    return ""


def find_tailscale_dir() -> str:
    """Locate a known-good tailscaled/tailscale arm64 pair.

    1.104.2 segfaults on this camera; 1.92.3 is verified, so prefer it.
    """
    roots = [HERE.parent / "vendor" / "tailscale", HERE / "tailscale"]
    candidates: list[str] = []
    for root in roots:
        candidates += glob.glob(str(root / "old" / "tailscale_*_arm64"))
        candidates += glob.glob(str(root / "tailscale_*_arm64"))
        candidates += glob.glob(str(root / "extract" / "tailscale_*_arm64"))
    candidates.append(str(HERE / "tailscale"))

    def ok(path: str) -> bool:
        return (os.path.isfile(os.path.join(path, "tailscaled")) and
                os.path.isfile(os.path.join(path, "tailscale")))

    good = [c for c in dict.fromkeys(candidates) if ok(c)]
    good.sort(key=lambda p: (0 if "1.92.3" in p else 1, len(p)))
    return good[0] if good else ""


def split_nmcli_line(line: str) -> list[str]:
    """Split nmcli -t output on unescaped colons."""
    fields: list[str] = []
    cur: list[str] = []
    esc = False
    for ch in line:
        if esc:
            cur.append(ch)
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ":":
            fields.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    fields.append("".join(cur))
    return fields


def parse_nmcli_scan(text: str) -> list[tuple[str, int, str]]:
    """Parse `nmcli -t -f SSID,SIGNAL,SECURITY device wifi list`."""
    best: dict[str, tuple[int, str]] = {}
    for line in text.splitlines():
        line = line.rstrip("\r")
        if not line:
            continue
        f = split_nmcli_line(line)
        if len(f) < 2:
            continue
        ssid = f[0].strip()
        if not ssid:
            continue
        try:
            signal = int(f[1])
        except ValueError:
            signal = 0
        security = f[2].strip() if len(f) > 2 else ""
        if security in ("--", "-- "):
            security = ""
        old = best.get(ssid)
        if old is None or signal > old[0]:
            best[ssid] = (signal, security)
    return sorted(((s, sig, sec) for s, (sig, sec) in best.items()),
                  key=lambda t: (-t[1], t[0].lower()))


def scan_wifi() -> list[tuple[str, int, str]]:
    exe = shutil.which("nmcli")
    if not exe:
        return []
    try:
        out = subprocess.run(
            [exe, "-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list"],
            capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return []
    return parse_nmcli_scan(out.stdout)


def probe_receiver(host: str, port: int, timeout: float = 2.5) -> tuple[bool, str]:
    """Ask the unauthenticated /api/status endpoint whether it is alive."""
    if not host:
        return False, "no server address"
    url = "http://%s:%d/api/status" % (host, port)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read(65536).decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, "%s: %s" % (url, exc)
    return True, "receiver reachable: %d files, %.1f GB stored" % (
        data.get("files_done", 0), data.get("bytes_done", 0) / (1 << 30))


def wifi_values(plan: Plan) -> dict[str, str]:
    values = {"WIFI_SSID": plan.ssid, "WIFI_PASSWORD": plan.password}
    if plan.country:
        values["WIFI_COUNTRY"] = plan.country
    return values


def render_wifi_conf(plan: Plan) -> str:
    return "".join("%s=%s\n" % (k, v) for k, v in wifi_values(plan).items())


def validate_wifi(ssid: str, password: str) -> str:
    """Return an error string, or '' when the stock parser can accept it."""
    if not ssid:
        return "an SSID is required"
    if "\n" in ssid or "\r" in ssid:
        return "the SSID must not contain a newline"
    if "\n" in password or "\r" in password:
        return "the password must not contain a newline"
    if any(ch in (ssid + password) for ch in ('"', "\\")):
        return "quotes and backslashes are not supported by the camera"
    return ""


# ---------------------------------------------------------------------------
# Curses helpers
# ---------------------------------------------------------------------------


def _addstr(scr, y: int, x: int, text: str, attr: int = 0) -> None:
    if y < 0 or x < 0:
        return
    try:
        scr.addstr(y, x, text, attr)
    except Exception:
        # Writing the bottom-right cell raises; clipping is acceptable.
        pass


def _title(scr, heading: str, subtitle: str = "") -> int:
    h, w = scr.getmaxyx()
    scr.erase()
    _addstr(scr, 0, 0, heading.ljust(w - 1), curses.A_REVERSE | curses.A_BOLD)
    if subtitle:
        _addstr(scr, 2, 2, subtitle[:w - 4])
    return 4


def _footer(scr, text: str) -> None:
    h, w = scr.getmaxyx()
    _addstr(scr, h - 1, 0, text.ljust(w - 1), curses.A_REVERSE)


def _menu(scr, heading: str, subtitle: str, items: list[str],
          footer: str, start: int = 0) -> int | None:
    """Arrow-key menu.  Returns the chosen index, or None for back/quit."""
    sel = max(0, min(start, len(items) - 1))
    while True:
        y = _title(scr, heading, subtitle)
        for i, item in enumerate(items):
            attr = curses.A_REVERSE if i == sel else 0
            _addstr(scr, y + i, 2, ("  " + item)[: scr.getmaxyx()[1] - 4], attr)
        _footer(scr, footer)
        scr.refresh()
        try:
            key = scr.getch()
        except curses.error:
            continue
        if key in (curses.KEY_UP, ord("k")):
            sel = (sel - 1) % len(items)
        elif key in (curses.KEY_DOWN, ord("j")) or key == ord("\t"):
            sel = (sel + 1) % len(items)
        elif key in (10, 13, curses.KEY_ENTER, curses.KEY_RIGHT):
            return sel
        elif key in (27, ord("q"), curses.KEY_LEFT):
            return None


def _prompt(scr, heading: str, subtitle: str, label: str,
            default: str = "", *, secret: bool = False) -> str | None:
    """One-line editor.  Secret input is rendered only as asterisks."""
    value = "" if secret else default
    while True:
        y = _title(scr, heading, subtitle)
        _addstr(scr, y, 2, label[: scr.getmaxyx()[1] - 4])
        shown = ("*" * len(value)) if secret else value
        _addstr(scr, y + 2, 4, shown[: scr.getmaxyx()[1] - 8])
        _addstr(scr, y + 2, 4 + min(len(shown), scr.getmaxyx()[1] - 8), "_")
        hint = "Enter to accept, Esc to go back"
        if not secret and default:
            hint += "  (default: %s)" % default
        _footer(scr, hint)
        scr.refresh()
        try:
            key = scr.get_wch()
        except curses.error:
            continue
        if key in ("\n", "\r"):
            return value
        if key == "\x1b":
            return None
        if key in ("\x7f", "\b", curses.KEY_BACKSPACE):
            value = value[:-1]
        elif key == "\x15":      # Ctrl-U
            value = ""
        elif isinstance(key, str) and key.isprintable():
            value += key


def _textbox(scr, heading: str, text: str) -> None:
    """Scrollable read-only viewer.  Enter/Esc/q closes."""
    lines = text.splitlines() or [""]
    top = 0
    while True:
        h, w = scr.getmaxyx()
        scr.erase()
        _addstr(scr, 0, 0, heading.ljust(w - 1), curses.A_REVERSE | curses.A_BOLD)
        body = h - 3
        for i in range(body):
            idx = top + i
            if idx >= len(lines):
                break
            _addstr(scr, 1 + i, 1, lines[idx][: w - 2])
        _footer(scr, "up/down/PgUp/PgDn scroll, Enter or q closes")
        scr.refresh()
        try:
            key = scr.getch()
        except curses.error:
            continue
        if key in (27, 10, 13, ord("q"), curses.KEY_ENTER):
            return
        if key in (curses.KEY_DOWN, ord("j")):
            top = min(max(0, len(lines) - body), top + 1)
        elif key in (curses.KEY_UP, ord("k")):
            top = max(0, top - 1)
        elif key == curses.KEY_NPAGE:
            top = min(max(0, len(lines) - body), top + body)
        elif key == curses.KEY_PPAGE:
            top = max(0, top - body)


def _yes_no(scr, heading: str, subtitle: str, label: str,
            default: bool = False) -> bool | None:
    idx = _menu(scr, heading, subtitle,
                ["Yes", "No"], label, start=0 if default else 1)
    if idx is None:
        return None
    return idx == 0


# ---------------------------------------------------------------------------
# Interactive flow
# ---------------------------------------------------------------------------


def _pick_card(scr) -> str | None:
    cards = detect_cards()
    if not cards:
        manual = _prompt(
            scr, "SD card", "No removable card was detected.",
            "Mountpoint: ", default="")
        return manual or None
    if len(cards) == 1:
        chosen = _yes_no(scr, "SD card",
                         "Detected %s" % cards[0],
                         "Use this card?", default=True)
        return cards[0] if chosen else None
    idx = _menu(scr, "SD card", "Choose the mounted camera card:", cards,
                "up/down selects, Enter accepts, q quits")
    return cards[idx] if idx is not None else None


def _existing_ssid(sd: str) -> str:
    path = Path(sd) / "wifi-sta.conf"
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("WIFI_SSID="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def _pick_wifi(scr, sd: str = "") -> tuple[str, str, str, bool] | None:
    """Return (ssid, password, country, remove_existing).

    An empty SSID with remove_existing=False means "leave the card's current
    profile alone".  None means the user cancelled the whole setup.
    """
    while True:
        nets = scan_wifi()
        preserved = _existing_ssid(sd)
        subtitle = ("Choose the network the camera should join.  At a hotel "
                    "this is your phone's hotspot.")
        entries: list[tuple[str, object]] = []
        items: list[str] = []
        if preserved:
            entries.append(("keep", None))
            items.append("Keep the card's current network: %s" % preserved)
        for net in nets:
            entries.append(("net", net))
        entries += [("rescan", None), ("manual", None), ("ap", None)]
        items += ["%s   (%d%%)%s" % (s, sig, "  " + sec if sec else "  open")
                  for s, sig, sec in nets]
        items += ["Rescan", "Enter SSID manually",
                  "Do not switch (keep camera AP)"]
        idx = _menu(scr, "Wi-Fi", subtitle, items,
                    "up/down selects, Enter accepts, q cancels setup")
        if idx is None:
            return None
        kind, net = entries[idx]
        if kind == "keep":
            return "", "", "", False
        if kind == "ap":
            return "", "", "", True
        if kind == "manual":
            ssid = _prompt(scr, "Wi-Fi", "Enter the network name exactly.",
                           "SSID: ", default="")
            if ssid is None:
                continue
            # nmcli is not describing this one, so ask instead of guessing.
            protected = _yes_no(
                scr, "Wi-Fi", "Networks are usually WPA2/WPA3 protected.",
                "Does %s use a password?" % ssid, default=True)
            if protected is None:
                continue
            security = "manual" if protected else ""
        elif kind == "rescan":
            continue
        else:
            ssid, _sig, security = net  # type: ignore[misc]

        password = ""
        if security:
            password = _prompt(
                scr, "Wi-Fi",
                "Password for %s (not shown, not saved on this computer)." % ssid,
                "Password: ", secret=True) or ""
            if password == "" and security:
                # Offer one retry rather than silently creating a bad config.
                again = _yes_no(scr, "Wi-Fi",
                                "No password entered for a protected network.",
                                "Use it anyway (open/WPS)?", default=False)
                if not again:
                    continue
        country = _prompt(
            scr, "Wi-Fi",
            "Optional regulatory country (CN is the camera default).",
            "Country (e.g. AU, blank for default): ", default="") or ""
        err = validate_wifi(ssid, password)
        if err:
            _textbox(scr, "Wi-Fi", "That value cannot be used:\n\n  " + err)
            continue
        return ssid, password, country.strip(), False


def _pick_server(scr, plan: Plan) -> bool:
    mode = _menu(
        scr, "Server", "How should the camera reach your home server?",
        ["Tailscale (recommended: works on any Wi-Fi, no port forwarding)",
         "LAN only (same network as the server)"],
        "up/down selects, Enter accepts, q goes back")
    if mode is None:
        return False
    plan.mode = "tailscale" if mode == 0 else "lan"

    if plan.mode == "tailscale":
        guessed = detect_tailnet_ip()
        target = _prompt(
            scr, "Server",
            "Tailnet IPv4 of the server (on the server: tailscale ip -4).",
            "Tailnet IP: ", default=guessed or plan.target)
        if target is None:
            return False
        plan.target = target.strip()
    else:
        ip = _prompt(scr, "Server", "IPv4 address of the home server.",
                     "Server IP: ", default=plan.server_ip)
        if ip is None:
            return False
        plan.server_ip = ip.strip()

    port = _prompt(scr, "Server",
                   "Receiver port (docker -p 8844:8844).",
                   "Port: ", default=str(plan.port))
    if port is None:
        return False
    try:
        plan.port = max(1, min(65535, int(port or "8844")))
    except ValueError:
        plan.port = 8844

    if plan.mode == "tailscale":
        state = Path(plan.sd) / "tailscale" / "tailscaled.state"
        if state.is_file():
            idx = _menu(
                scr, "Tailscale login",
                "A saved Tailscale login already exists on this card:\n\n"
                "  %s\n\n"
                "Keep it unless the node was removed in the Tailscale admin "
                "console or its key expired." % state,
                ["Keep the saved login",
                 "Replace it with a new auth key"],
                "up/down selects, Enter accepts, q goes back")
            if idx is None:
                return False
            if idx == 1:
                key = _prompt(
                    scr, "Tailscale login",
                    "Paste a reusable auth key.  The saved node state will be "
                    "removed so the camera logs in as a fresh node.",
                    "Auth key (hidden): ", secret=True)
                if key is None:
                    return False
                plan.authkey = key.strip()
                plan.reset_tailscale = bool(plan.authkey)
        else:
            key = _prompt(
                scr, "Tailscale login",
                "No saved login.  Generate a reusable auth key at "
                "login.tailscale.com/admin/settings/keys, or leave blank "
                "to add one later in cloud-backup.conf.",
                "Auth key (hidden): ", secret=True)
            if key is None:
                return False
            plan.authkey = key.strip()

    ok, msg = probe_receiver(plan.effective_server(), plan.port)
    _textbox(scr, "Server reachability",
             ("Reachable.\n\n  %s\n" % msg) if ok else
             ("Not reachable right now.\n\n  %s\n\n"
              "That is only a warning: the camera may reach it later over "
              "the tailnet.  Check the address and port before continuing."
              % msg))
    return True


def _pick_token(scr, plan: Plan) -> bool:
    default_file = HERE / TOKEN_NAME
    have_default = default_file.is_file()
    items = []
    if have_default:
        items.append("Use existing %s" % default_file)
    items += ["Generate a new random token",
              "Choose a token file", "Skip (no token; receiver must be open)"]
    idx = _menu(scr, "Upload token",
                "The camera and the receiver must share this secret.",
                items, "up/down selects, Enter accepts, q goes back")
    if idx is None:
        return False
    label = items[idx]
    if label.startswith("Use existing"):
        plan.token_file = str(default_file)
    elif label.startswith("Generate"):
        token = secrets.token_hex(24)
        fd, path = tempfile.mkstemp(prefix="insta360-token-", suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(token + "\n")
        os.chmod(path, 0o600)
        plan.token_file = path
        _textbox(scr, "Upload token",
                 "Generated a new token.  Configure the receiver with the "
                 "same value:\n\n"
                 "  docker exec insta360-sink cat /app/sd-token\n\n"
                 "or pass this token to the receiver once:\n\n  %s\n\n"
                 "The token is in a temporary file and will be written to the "
                 "card as sd-upload.token." % token)
    elif label.startswith("Choose"):
        path = _prompt(scr, "Upload token", "Path to a file holding the token.",
                       "Token file: ", default="")
        if not path or not os.path.isfile(path):
            return False
        plan.token_file = path
    else:
        plan.token_file = ""
    return True


def _pick_options(scr, plan: Plan) -> bool:
    while True:
        idx = _menu(
            scr, "Options", "Upload behaviour on the camera:",
            ["Delete from the card after the server confirms a file: %s"
             % ("yes" if plan.delete_after else "no"),
             "Uploader retry interval: %d s" % plan.interval,
             "Continue"],
            "up/down selects, Enter changes or continues, q goes back")
        if idx is None:
            return False
        if idx == 0:
            plan.delete_after = not plan.delete_after
        elif idx == 1:
            val = _prompt(scr, "Options", "Seconds between upload passes.",
                          "Interval: ", default=str(plan.interval))
            if val:
                try:
                    plan.interval = max(10, min(86400, int(val)))
                except ValueError:
                    pass
        else:
            return True


def _summary(plan: Plan) -> str:
    wifi = ("%s (password set)" % plan.ssid if plan.password else
            (plan.ssid or "keep the camera's own AP"))
    return (
        "SD card:      %s\n"
        "Wi-Fi:        %s\n"
        "Server mode:  %s\n"
        "Server:       %s\n"
        "Receiver:     %s\n"
        "Token file:   %s\n"
        "Tailscale:    %s\n"
        "Tailscale login: %s\n"
        "Delete after upload: %s\n"
        "Retry interval: %d s\n"
        "\n"
        "After install the card is safe to remove when the tool says so.\n"
        "The camera keeps its normal hotspot unless it joins this network."
        % (plan.sd, wifi, plan.mode, plan.effective_server() or "<missing>",
           plan.upload_url(),
           plan.token_file or "none",
           plan.tailscale_dir or "not found (installer will warn)",
           (("new auth key; saved login will be replaced"
             if plan.reset_tailscale else
             "new auth key supplied") if plan.authkey else
            "saved state on card / add a key later"),
           "yes" if plan.delete_after else "no", plan.interval))


def _run_steps(scr, plan: Plan, wifi_conf: str) -> tuple[bool, str]:
    authkey_file = ""
    if plan.authkey:
        fd, authkey_file = tempfile.mkstemp(prefix="insta360-authkey-")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(plan.authkey + "\n")
        os.chmod(authkey_file, 0o600)
    try:
        return _run_steps_inner(scr, plan, wifi_conf, authkey_file)
    finally:
        if authkey_file:
            try:
                os.unlink(authkey_file)
            except OSError:
                pass


def _run_steps_inner(scr, plan: Plan, wifi_conf: str,
                     authkey_file: str) -> tuple[bool, str]:
    reset_note = ""
    if plan.reset_tailscale:
        ts_dir = Path(plan.sd) / "tailscale"
        for path in [ts_dir / "tailscaled.state", ts_dir / ".logged-in",
                     *ts_dir.glob("tailscaled.state.tmp*")]:
            try:
                path.unlink()
                reset_note += "removed %s\n" % path
            except FileNotFoundError:
                pass
            except OSError as exc:
                return False, "could not remove %s: %s" % (path, exc)
    argv = build_install_argv(plan, wifi_conf=wifi_conf,
                              token_file=plan.token_file,
                              authkey_file=authkey_file)
    _title(scr, "Installing", "Copying the boot hook and uploader to the card...")
    _footer(scr, "this can take a minute")
    scr.refresh()
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, "install failed to start:\n\n%s" % exc
    out = "$ %s\n\n%s\n%s" % (" ".join(argv), proc.stdout, proc.stderr)
    if reset_note:
        out = reset_note + "\n" + out
    if proc.returncode != 0:
        out += "\n\ninstall_to_sd.sh exited %d" % proc.returncode
        return False, out

    # install_to_sd.sh keeps an existing cloud-backup.conf, so apply the
    # runtime keys here rather than relying on its first-write behaviour.
    conf_path = Path(plan.sd) / CONF_NAME
    try:
        text = conf_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    values = conf_values(plan, has_token=bool(plan.token_file))
    try:
        write_text(conf_path, update_conf_text(text, values))
        os.sync()
        out += "\n\nupdated %s:\n" % conf_path
        out += "\n".join("  %s=%s" % (k, v) for k, v in values.items())
    except OSError as exc:
        out += "\n\nWARNING: could not update %s: %s" % (conf_path, exc)

    # Verify as a separate step so its output is visible even on success.
    verify = ["sh", str(HERE / "verify_sd.sh"), plan.sd]
    try:
        vp = subprocess.run(verify, capture_output=True, text=True, timeout=120)
        out += "\n\n$ %s\n\n%s\n%s" % (" ".join(verify), vp.stdout, vp.stderr)
        if vp.returncode != 0:
            out += "\n\nverify_sd.sh reported errors (exit %d)" % vp.returncode
            return False, out
    except (OSError, subprocess.SubprocessError) as exc:
        out += "\n\nWARNING: verify_sd.sh did not run: %s" % exc
    return True, out


def _checklist(plan: Plan) -> str:
    lines = [
        "The card is ready.",
        "",
        "1. Eject it safely:",
        "     sync",
        "     udisksctl unmount -b /dev/mmcblk0p1",
        "",
        "2. Insert it into the X4 Air and power on.",
        "",
        "3. If you chose a Wi-Fi network, the camera joins it and its own",
        "   hotspot disappears.  The app will disconnect: this is expected.",
        "   If the network is not visible, the camera restores its normal",
        "   hotspot after about 15 seconds and uploads nothing that boot.",
        "",
        "4. Watch progress:",
        "     %s/" % plan.upload_url(),
        "   or the boot log at %s/bootup.log on the card." % plan.sd,
        "",
        "5. The receiver stores files under its /data volume.  Keep the",
        "   camera powered while a clip is uploading.",
    ]
    if plan.delete_after:
        lines += ["", "Delete-after-upload is ON: the camera removes files",
                  "only after the server confirms the final size."]
    if plan.mode == "tailscale":
        lines += ["", "Tailscale must be logged in on the camera once;",
                  "tailscale-up.sh handles that with TAILSCALE_AUTHKEY."]
    return "\n".join(lines)


def plan_from_args(args: argparse.Namespace) -> Plan:
    """Pre-seed the interactive plan from the command line.

    Every value is still shown and confirmed in the TUI; --sd only skips the
    "which card?" prompt.
    """
    return Plan(
        sd=args.sd or "",
        mode=args.mode,
        target=args.target or "",
        server_ip=args.server_ip or "",
        port=args.port,
        token_file=args.token_file or "",
        tailscale_dir=args.tailscale_dir or find_tailscale_dir(),
        delete_after=args.delete_after,
        interval=args.interval,
        lan_url=args.lan_url or "",
        no_wifi=args.no_wifi,
    )


def interactive(args: argparse.Namespace | None = None) -> int:
    if curses is None:  # pragma: no cover
        print("curses is not available; use --non-interactive",
              file=sys.stderr)
        return 2

    plan = (plan_from_args(args) if args is not None
            else Plan(tailscale_dir=find_tailscale_dir()))
    default_token = HERE / TOKEN_NAME
    if not plan.token_file and default_token.is_file():
        plan.token_file = str(default_token)

    def app(scr):
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        scratch = tempfile.mkdtemp(prefix="insta360-setup-")
        wifi_conf = ""
        try:
            if plan.sd:
                if not os.path.isdir(plan.sd):
                    _textbox(scr, "SD card",
                             "That path is not a mounted directory:\n\n  %s"
                             % plan.sd)
                    return 1
            else:
                sd = _pick_card(scr)
                if not sd:
                    return 1
                plan.sd = sd

            if plan.no_wifi:
                picked = ("", "", "", True)
            else:
                picked = _pick_wifi(scr, plan.sd)
            if picked is None:
                return 1
            ssid, password, country, no_wifi = picked
            plan.ssid, plan.password, plan.country = ssid, password, country
            plan.no_wifi = no_wifi
            if ssid:
                wifi_conf = os.path.join(scratch, "wifi-sta.local")
                write_text(Path(wifi_conf), render_wifi_conf(plan), 0o600)

            if not _pick_server(scr, plan):
                return 1
            if not _pick_token(scr, plan):
                return 1
            if not _pick_options(scr, plan):
                return 1

            while True:
                idx = _menu(scr, "Ready",
                            "Review the plan.  Nothing has been written yet.",
                            ["Show summary", "Install now", "Cancel"],
                            "up/down selects, Enter accepts, q cancels")
                if idx is None or idx == 2:
                    return 1
                if idx == 0:
                    _textbox(scr, "Summary", _summary(plan))
                    continue
                ok, out = _run_steps(scr, plan, wifi_conf)
                _textbox(scr, "Install result", out)
                if ok:
                    _textbox(scr, "Checklist", _checklist(plan))
                    return 0
                again = _yes_no(scr, "Install failed",
                                "The card was not fully prepared.",
                                "Show the log again?", default=True)
                if again:
                    _textbox(scr, "Install result", out)
                return 1
        finally:
            # The password lives only in memory and in this 0600 file.
            shutil.rmtree(scratch, ignore_errors=True)
            plan.password = ""

    return curses.wrapper(app)


# ---------------------------------------------------------------------------
# Non-interactive flow
# ---------------------------------------------------------------------------


def _read_secret_file(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read().rstrip("\n")


def non_interactive(args: argparse.Namespace) -> int:
    plan = Plan(
        sd=args.sd,
        mode=args.mode,
        target=args.target or "",
        server_ip=args.server_ip or "",
        port=args.port,
        token_file=args.token_file or "",
        authkey=_read_secret_file(args.authkey_file) if args.authkey_file else "",
        tailscale_dir=args.tailscale_dir or find_tailscale_dir(),
        delete_after=args.delete_after,
        interval=args.interval,
        lan_url=args.lan_url or "",
        no_wifi=args.no_wifi,
        country=args.wifi_country or "",
    )
    scratch = None
    wifi_conf = args.wifi_conf or ""
    try:
        if args.wifi_ssid:
            if not args.wifi_password_file:
                print("--wifi-ssid also needs --wifi-password-file",
                      file=sys.stderr)
                return 2
            plan.ssid = args.wifi_ssid
            plan.password = _read_secret_file(args.wifi_password_file)
            err = validate_wifi(plan.ssid, plan.password)
            if err:
                print(err, file=sys.stderr)
                return 2
            scratch = tempfile.mkdtemp(prefix="insta360-setup-")
            wifi_conf = os.path.join(scratch, "wifi-sta.local")
            write_text(Path(wifi_conf), render_wifi_conf(plan), 0o600)

        try:
            argv = build_install_argv(plan, wifi_conf=wifi_conf,
                                      token_file=plan.token_file,
                                      authkey_file=args.authkey_file or "")
        except ValueError as exc:
            print("invalid plan: %s" % exc, file=sys.stderr)
            return 2

        values = conf_values(plan, has_token=bool(plan.token_file))
        if args.dry_run:
            print("install argv:")
            for part in argv:
                print("  " + part)
            print("conf updates:")
            for key, val in values.items():
                print("  %s=%s" % (key, val))
            return 0

        print("installing ...", flush=True)
        proc = subprocess.run(argv, text=True)
        if proc.returncode != 0:
            print("install_to_sd.sh exited %d" % proc.returncode,
                  file=sys.stderr)
            return proc.returncode

        conf_path = Path(plan.sd) / CONF_NAME
        text = conf_path.read_text(encoding="utf-8", errors="replace")
        write_text(conf_path, update_conf_text(text, values))
        os.sync()
        print("updated %s" % conf_path)

        vp = subprocess.run(["sh", str(HERE / "verify_sd.sh"), plan.sd],
                            text=True)
        if vp.returncode != 0:
            print("verify_sd.sh reported errors", file=sys.stderr)
            return 1
        print()
        print(_checklist(plan))
        return 0
    finally:
        if scratch:
            shutil.rmtree(scratch, ignore_errors=True)
        plan.password = ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sd", help="mounted card (skips detection)")
    ap.add_argument("--ssid", dest="wifi_ssid")
    ap.add_argument("--wifi-password-file",
                    help="file containing the Wi-Fi password (never argv)")
    ap.add_argument("--wifi-conf", help="pre-made wifi-sta.conf")
    ap.add_argument("--wifi-country")
    ap.add_argument("--no-wifi", action="store_true",
                    help="remove an existing station-mode profile")
    ap.add_argument("--mode", choices=("tailscale", "lan"), default="tailscale")
    ap.add_argument("--target", help="tailnet IP of the server")
    ap.add_argument("--server-ip", help="LAN IP of the server")
    ap.add_argument("--port", type=int, default=8844)
    ap.add_argument("--token-file")
    ap.add_argument("--authkey-file",
                    help="file with a Tailscale auth key (never argv)")
    ap.add_argument("--tailscale-dir")
    ap.add_argument("--delete-after", action="store_true")
    ap.add_argument("--interval", type=int, default=60)
    ap.add_argument("--lan-url")
    ap.add_argument("--non-interactive", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    if args.non_interactive:
        if not args.sd:
            print("--non-interactive requires --sd", file=sys.stderr)
            return 2
        return non_interactive(args)
    if args.dry_run:
        print("--dry-run needs --non-interactive", file=sys.stderr)
        return 2
    return interactive(args)


if __name__ == "__main__":
    sys.exit(main())
