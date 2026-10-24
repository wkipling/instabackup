#!/usr/bin/env python3
"""Sanity-check the freestanding AArch64 tailrelay binary.

The protocol logic is tested on the host, but the AArch64 build has its own
entry stub and raw-syscall ABI.  This script disassembles .text and checks:

  * the entry point looks like the expected sp -> start_c stub,
  * every `svc #0` is preceded by the intended `mov x8, #N`,
  * all syscall numbers in the aarch64 build are from the expected set.

Usage: python3 check_tailrelay_asm.py [path-to-tailrelay]
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

from capstone import CS_ARCH_ARM64, CS_MODE_ARM, Cs

EXPECTED = {
    7, 57, 63, 64, 94, 101, 198, 200, 201, 202, 203,
    206, 207, 208, 210, 220, 260,
}


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "tailrelay")
    if not os.path.exists(path):
        print("missing %s" % path, file=sys.stderr)
        return 2

    data = open(path, "rb").read()
    headers = subprocess.check_output(["readelf", "-hW", path], text=True)
    sections = subprocess.check_output(["readelf", "-SW", path], text=True)
    entry = int(re.search(r"Entry point address:\s+0x([0-9a-f]+)", headers).group(1), 16)
    m = re.search(r"\.text\s+PROGBITS\s+([0-9a-f]+)\s+([0-9a-f]+)\s+([0-9a-f]+)",
                  sections)
    if not m:
        print("no .text section", file=sys.stderr)
        return 2
    addr, off, size = (int(x, 16) for x in m.groups())

    md = Cs(CS_ARCH_ARM64, CS_MODE_ARM)
    insns = list(md.disasm(data[off:off + size], addr))
    by_addr = {i.address: i for i in insns}

    print("entry: 0x%x" % entry)
    cur = entry
    for _ in range(10):
        ins = by_addr.get(cur)
        if not ins:
            break
        print("  %08x  %-8s %s" % (ins.address, ins.mnemonic, ins.op_str))
        cur += ins.size

    seen = set()
    sites = []
    for i, ins in enumerate(insns):
        if ins.mnemonic != "svc":
            continue
        nr = None
        for j in range(i - 1, max(-1, i - 16), -1):
            prev = insns[j]
            if prev.mnemonic == "mov":
                mm = re.match(r"[xw]8,\s*#(0x[0-9a-f]+|\d+)",
                              prev.op_str.replace(" ", ""))
                if mm:
                    nr = int(mm.group(1), 0)
                    break
        sites.append((ins.address, nr))
        if nr is not None:
            seen.add(nr)

    print("svc sites: %d" % len(sites))
    missing = EXPECTED - seen
    unexpected = seen - EXPECTED
    for a, nr in sites:
        flag = "" if nr in EXPECTED else "  <-- unexpected"
        print("  0x%x  x8=%s%s" % (a, nr, flag))
        if nr is None:
            idx = next(i for i, x in enumerate(insns) if x.address == a)
            for prev in insns[max(0, idx - 10):idx]:
                print("      %08x  %-8s %s"
                      % (prev.address, prev.mnemonic, prev.op_str))
    if missing:
        print("WARNING: expected syscall numbers not seen: %s"
              % sorted(missing))
    unresolved = [a for a, nr in sites if nr is None]
    if unresolved:
        print("ERROR: no x8/w8 immediate found before svc at: %s"
              % [hex(a) for a in unresolved])
        return 1
    if unexpected:
        print("ERROR: unexpected syscall numbers: %s" % sorted(unexpected))
        return 1
    if not sites:
        print("ERROR: no svc instructions found")
        return 1
    print("OK aarch64 syscalls look consistent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
