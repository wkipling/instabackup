#!/usr/bin/env python3
"""Verify the Insta360 X4 Air 1.2.7 firmware facts used by the SD-card hook.

Checks, against the official image:

  * MD5 / size match Insta360's download manifest;
  * the rootfs is a 278-PEB UBIFS volume at 0xe1caa87;
  * /etc/init.d/S99bootdone runs /tmp/SD0/bootup.sh unconditionally;
  * /usr/bin/instaAIP and /usr/lib/libInsCloudSdk.so are present;
  * instaAIP uses the service-test.insta360.com/cloud-service API base;
  * the `cloud_msg` SysV queue contract (key, mode, mtype, payload size,
    action offset) is the one `cloudkick` implements;
  * the SDK contains the upload endpoints the local server implements;
  * ambernation.sh honours /tmp/SD0/NO_HIBER.

The queue checks use a fixed code address. They are deliberately tied to
version 1.2.7 (the MD5 gate); a newer image needs a fresh Ghidra look rather
than silently reusing stale offsets.

Usage:
    python3 verify_x4air_fw.py
    python3 verify_x4air_fw.py --firmware path/to/Insta360X4AirFW_1.2.7.bin
    python3 verify_x4air_fw.py --root /tmp/extracted/.../linux  # skip UBIFS
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

EXPECTED_MD5 = "53a8d0ebb937a41ef0edc92bdf556f40"
EXPECTED_SIZE = 309_943_867
UBI_OFFSET = 0xE1CAA87
PEB_SIZE = 0x40000
PEB_COUNT = 278
QUEUE_SETUP = 0x4C360
QUEUE_CREATE = 0x44EE0
QUEUE_SEND = 0x44F00
QUEUE_KICK = 0x4BF80

HERE = Path(__file__).resolve().parent
DEFAULT_FW = HERE.parent / "vendor/firmware/x4-air/Insta360X4AirFW_1.2.7.bin"


class Checker:
    def __init__(self) -> None:
        self.failed = 0

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        print(("  OK   " if ok else "  FAIL ") + name + (f" ({detail})" if detail else ""))
        if not ok:
            self.failed += 1


def md5_file(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def extract_rootfs(fw: Path, work: Path) -> Path:
    tool = shutil.which("ubireader_extract_files")
    if tool is None:
        raise SystemExit(
            "ubireader_extract_files not found. Install ubi_reader or pass "
            "--root /path/to/extracted/linux."
        )
    ubi = work / "rootfs.ubi"
    try:
        with fw.open("rb") as src, ubi.open("wb") as dst:
            src.seek(UBI_OFFSET)
            remaining = PEB_COUNT * PEB_SIZE
            while remaining:
                chunk = src.read(min(1 << 20, remaining))
                if not chunk:
                    raise SystemExit("short read while slicing rootfs")
                dst.write(chunk)
                remaining -= len(chunk)
    except OSError as exc:
        raise SystemExit(
            f"cannot write the extracted rootfs: {exc}; pass --work-dir on a "
            "filesystem with at least ~250 MB free"
        ) from exc
    out = work / "root"
    subprocess.run([tool, "-o", str(out), str(ubi)], check=True)
    roots = sorted(out.glob("*/linux"))
    if not roots:
        raise SystemExit("ubireader produced no */linux root")
    return roots[0]


def disasm(binary: bytes, offset: int, count: int):
    try:
        from capstone import Cs, CS_ARCH_ARM64, CS_MODE_LITTLE_ENDIAN
    except ImportError as exc:
        raise SystemExit("capstone is required for queue checks") from exc
    md = Cs(CS_ARCH_ARM64, CS_MODE_LITTLE_ENDIAN)
    return list(md.disasm(binary[offset:offset + count * 4], offset))


def require_insns(checker: Checker, name: str, insns, wanted: list[tuple[str, str]]) -> None:
    """Require each (mnemonic, operand substring) pair in the instruction window."""
    ok = True
    for mnemonic, needle in wanted:
        if not any(i.mnemonic == mnemonic and needle in i.op_str for i in insns):
            ok = False
            break
    checker.check(name, ok, "; ".join(f"{m} {n}" for m, n in wanted))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--firmware", type=Path, default=DEFAULT_FW)
    ap.add_argument("--root", type=Path,
                    help="already-extracted rootfs linux/ directory")
    ap.add_argument("--work-dir", type=Path,
                    help="keep the extracted UBIFS here instead of a temp dir")
    args = ap.parse_args()

    ck = Checker()
    fw = args.firmware
    print(f"firmware: {fw}")
    ck.check("firmware exists", fw.is_file(), str(fw))
    if not fw.is_file():
        return 1
    size = fw.stat().st_size
    ck.check("size", size == EXPECTED_SIZE, f"{size} bytes")
    digest = md5_file(fw)
    ck.check("MD5", digest == EXPECTED_MD5, digest)
    if size != EXPECTED_SIZE or digest != EXPECTED_MD5:
        print("\nThis verifier is pinned to X4 Air 1.2.7; refusing to apply "
              "its code offsets to a different image.")
        return 1

    data = fw.read_bytes()
    ck.check("UBI magic at 0xe1caa87", data[UBI_OFFSET:UBI_OFFSET + 4] == b"UBI#")
    ck.check("UBI PEB chain", data[UBI_OFFSET + PEB_SIZE:UBI_OFFSET + PEB_SIZE + 4] == b"UBI#")

    tmp = None
    if args.root:
        root = args.root
    else:
        if args.work_dir:
            args.work_dir.mkdir(parents=True, exist_ok=True)
            work = args.work_dir
        else:
            tmp = tempfile.TemporaryDirectory(prefix="x4air-fw-")
            work = Path(tmp.name)
        root = extract_rootfs(fw, work)
    print(f"rootfs:   {root}")

    boot = root / "etc/init.d/S99bootdone"
    aip = root / "usr/bin/instaAIP"
    sdk = root / "usr/lib/libInsCloudSdk.so"
    amber = root / "usr/local/share/script/ambernation.sh"
    for p in (boot, aip, sdk, amber):
        ck.check(f"present {p.relative_to(root)}", p.is_file())

    boot_text = boot.read_text(errors="replace") if boot.is_file() else ""
    ck.check("S99bootdone has uncommented SD0 test",
             bool(re.search(r"(?m)^\s*if \[ -x /tmp/SD0/bootup\.sh \]; then", boot_text)))
    ck.check("S99bootdone executes /tmp/SD0/bootup.sh",
             bool(re.search(r"(?m)^\s*/tmp/SD0/bootup\.sh\s*$", boot_text)))
    ck.check("S99bootdone supports /tmp/SD0/instaAIP override",
             "/tmp/SD0/instaAIP" in boot_text)

    amber_text = amber.read_text(errors="replace") if amber.is_file() else ""
    ck.check("ambernation.sh honours NO_HIBER", "/tmp/SD0/NO_HIBER" in amber_text)

    aip_b = aip.read_bytes() if aip.is_file() else b""
    sdk_b = sdk.read_bytes() if sdk.is_file() else b""
    for needle, label in [
        (b"cloud_msg", "instaAIP cloud_msg thread"),
        (b"https://service-test.insta360.com/cloud-service", "service-test API base"),
        (b"/tmp/SD0/DCIM/", "DCIM scan path"),
        (b"[ins_cloud] recv upload action:%d", "action dispatch log"),
        (b"/upload_file", "instaAIP web upload endpoint (unused by this path)"),
    ]:
        ck.check(label, needle in aip_b)
    for needle in [
        b"/cloud/service/camera/upload/config",
        b"/cloud/service/camera/upload/derivatives",
        b"/cloud/service/camera/upload/item/entrance?",
        b"/cloud/service/camera/upload/item/status/report",
        b"/cloud/service/camera/userInfo/binding/status",
        b"/cloud/service/camera/media/uploaded/list?",
    ]:
        ck.check("SDK endpoint " + needle.decode(), needle in sdk_b)

    # Queue setup: msgget(0x44455255, 0x3b6) in the cloud_msg thread.
    if aip_b:
        setup = disasm(aip_b, QUEUE_SETUP, 40)
        require_insns(ck, "queue key 0x44455255", setup, [
            ("mov", "#0x5255"),
            ("movk", "#0x4445, lsl #16"),
        ])
        create = disasm(aip_b, QUEUE_CREATE, 8)
        require_insns(ck, "queue mode 0x3b6", create, [("mov", "#0x3b6")])
        send = disasm(aip_b, QUEUE_SEND, 8)
        require_insns(ck, "message text size 0x408", send, [("mov", "#0x408")])
        kick = disasm(aip_b, QUEUE_KICK, 28)
        require_insns(ck, "mtype/mtext[0]=1 and action at mtext[4]", kick, [
            ("mov", "#1"),
            ("str", "[sp, #0x28]"),
            ("str", "[sp, #0x30]"),
            ("strb", "[sp, #0x34]"),
        ])
        dispatch = disasm(aip_b, 0x4C470, 0x210 // 4)
        require_insns(ck, "action 7 dispatch (scan DCIM)", dispatch, [
            ("cmp", "#7"),
            ("bl", "#0x52da0"),
        ])
        require_insns(ck, "action 8 dispatch (query uploaded)", dispatch, [
            ("cmp", "#8"),
            ("bl", "#0x52f40"),
        ])
        require_insns(ck, "action 0 dispatch (start upload)", dispatch, [
            ("cbnz", "w26"),
            ("bl", "#0x4edb0"),
        ])
        require_insns(ck, "action 15 dispatch (save DB)", dispatch, [
            ("cmp", "#0xf"),
            ("bl", "#0x4e308"),
        ])

    if tmp is not None:
        tmp.cleanup()

    print()
    if ck.failed:
        print(f"RESULT: {ck.failed} check(s) failed")
        return 1
    print("RESULT: X4 Air 1.2.7 is compatible with the SD-card cloud-backup kit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
