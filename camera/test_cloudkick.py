#!/usr/bin/env python3
"""Tiny AArch64 emulator for the exact subset used by cloudkick.

This is a build-time sanity test, not a general CPU emulator.  It verifies
that the hand-assembled ELF actually posts mtype=1, size=0x408, action at
mtext[4], in argv order.
"""

import re
import struct
import sys

from capstone import Cs, CS_ARCH_ARM64, CS_MODE_ARM

BASE = 0x400000
ENTRY = 0x400100
SP0 = 0x800000
ARGV0 = 0x900100
ARGV1 = 0x900000


def emu(blob, argv1):
    md = Cs(CS_ARCH_ARM64, CS_MODE_ARM)
    code = blob[0x100:]
    x = [0] * 31
    sp = SP0
    flags = {"n": 0, "z": 0, "c": 0, "v": 0}
    mem = {}
    for i, b in enumerate(blob):
        mem[BASE + i] = b

    def rd(addr, size):
        return b"".join(bytes([mem.get(addr + i, 0)]) for i in range(size))

    def wr(addr, data):
        for i, b in enumerate(data):
            mem[addr + i] = b

    def getreg(name):
        name = name.strip()
        if name == "sp":
            return sp
        if name == "xzr":
            return 0
        m = re.fullmatch(r"[xw](\d+)", name)
        if not m:
            raise ValueError(name)
        n = int(m.group(1))
        return x[n] & (0xFFFFFFFF if name[0] == "w" else 0xFFFFFFFFFFFFFFFF)

    def setreg(name, val):
        nonlocal sp
        name = name.strip()
        if name == "sp":
            sp = val & 0xFFFFFFFFFFFFFFFF
            return
        m = re.fullmatch(r"[xw](\d+)", name)
        if not m:
            raise ValueError(name)
        n = int(m.group(1))
        x[n] = val & (0xFFFFFFFF if name[0] == "w" else 0xFFFFFFFFFFFFFFFF)

    def setflags(a, b, is64=True):
        mask = 0xFFFFFFFFFFFFFFFF if is64 else 0xFFFFFFFF
        r = (a - b) & mask
        flags["z"] = 1 if r == 0 else 0
        flags["n"] = (r >> (63 if is64 else 31)) & 1
        flags["c"] = 1 if a >= b else 0
        flags["v"] = 0

    wr(SP0, struct.pack("<Q", 2 if argv1 is not None else 1))
    wr(SP0 + 8, struct.pack("<Q", ARGV0))
    wr(ARGV0, b"cloudkick\0")
    if argv1 is not None:
        wr(SP0 + 16, struct.pack("<Q", ARGV1))
        wr(ARGV1, argv1.encode() + b"\0")
    sent = []
    pc = ENTRY
    for _ in range(100000):
        ins = next(md.disasm(rd(pc, 4), pc, 1), None)
        if ins is None:
            raise AssertionError(f"bad instruction at {pc:#x}")
        m, o = ins.mnemonic, ins.op_str
        if __import__("os").environ.get("DEBUG"):
            print(f"{ins.address:08x} {m} {o}  x4={x[4]:#x} x21={x[21]:#x} x22={x[22]:#x} sp={sp:#x} z={flags['z']}")
        pc += 4
        if m == "svc":
            n = x[8]
            if n == 186:                    # msgget
                x[0] = 3
            elif n == 189:                  # msgsnd
                qid, msgp, size, flg = x[0], x[1], x[2], x[3]
                mtype = struct.unpack("<Q", rd(msgp, 8))[0]
                first = struct.unpack("<I", rd(msgp + 8, 4))[0]
                action = rd(msgp + 12, 1)[0]
                sent.append((qid, mtype, first, size, flg, action))
                x[0] = 0
            elif n == 93:                   # exit
                return x[0], sent
            else:
                raise AssertionError(f"unexpected syscall {n}")
            continue
        if m == "mov":
            a, b = [z.strip() for z in o.split(",", 1)]
            if b.startswith("#"):
                setreg(a, int(b[1:], 0))
            else:
                setreg(a, getreg(b))
        elif m == "movk":
            a, rest = [z.strip() for z in o.split(",", 1)]
            imm, shift = rest.split(",")
            sh = int(shift.split("#")[1])
            cur = getreg(a)
            val = (cur & ~(0xFFFF << sh)) | ((int(imm.split("#")[1], 0) & 0xFFFF) << sh)
            setreg(a, val)
        elif m in ("cmp", "subs", "sub", "add"):
            parts = [z.strip() for z in o.split(",")]
            if len(parts) == 2:
                a, b = parts
                dst = a
                src = a
                rhs_s = b
            else:
                dst, src, rhs_s = parts
            rhs = int(rhs_s[1:], 0) if rhs_s.startswith("#") else getreg(rhs_s)
            lhs = getreg(src)
            is64 = src.startswith("x") or src == "sp"
            val = (lhs - rhs) if m in ("cmp", "subs", "sub") else (lhs + rhs)
            if m in ("cmp", "subs"):
                setflags(lhs, rhs, is64)
            if m != "cmp":
                setreg(dst, val)
        elif m in ("ldr", "ldrb"):
            m2 = re.fullmatch(r"\s*([xw]\d+),\s*\[(sp|x\d+)(?:,\s*#(0x[0-9a-f]+|\d+))?\](?:,\s*#(\d+))?", o)
            if not m2:
                raise AssertionError(o)
            dst, base, off, post = m2.groups()
            addr = getreg(base) + (int(off, 0) if off else 0)
            if m == "ldrb":
                setreg(dst, rd(addr, 1)[0])
            else:
                size = 8 if dst.startswith("x") else 4
                setreg(dst, int.from_bytes(rd(addr, size), "little"))
            if post:
                setreg(base, getreg(base) + int(post))
        elif m in ("str", "strb"):
            if o.strip().startswith("xzr,"):
                dst, rest = "xzr", o.split(",", 1)[1]
            else:
                dst, rest = [z.strip() for z in o.split(",", 1)]
            m2 = re.fullmatch(r"\s*\[(sp|x\d+)(?:,\s*#(0x[0-9a-f]+|\d+))?\](?:,\s*#(\d+))?", rest)
            if not m2:
                raise AssertionError(o)
            base, off, post = m2.groups()
            addr = getreg(base) + (int(off, 0) if off else 0)
            val = getreg(dst)
            if m == "strb":
                wr(addr, bytes([val & 0xFF]))
            else:
                # str with a w source stores 4 bytes
                size = 4 if dst.startswith("w") else 8
                wr(addr, (val & ((1 << (size * 8)) - 1)).to_bytes(size, "little"))
            if post:
                setreg(base, getreg(base) + int(post))
        elif m == "cbz":
            a, target = [z.strip() for z in o.split(",", 1)]
            if getreg(a) == 0:
                pc = int(target[1:], 16)
        elif m == "b":
            pc = int(o.strip()[1:], 16)
        elif m.startswith("b."):
            cond = m[2:]
            take = {
                "eq": flags["z"] == 1,
                "ne": flags["z"] == 0,
                "lt": flags["n"] != flags["v"],
                "gt": flags["z"] == 0 and flags["n"] == flags["v"],
                "le": flags["z"] == 1 or flags["n"] != flags["v"],
                "ge": flags["n"] == flags["v"],
            }[cond]
            if take:
                pc = int(o.strip()[1:], 16)
        else:
            raise AssertionError(f"unhandled {m} {o}")
    raise AssertionError("did not exit")


def main():
    blob = open(sys.argv[1] if len(sys.argv) > 1 else "cloudkick", "rb").read()
    rc, sent = emu(blob, "870")
    assert rc == 0, rc
    assert [s[5] for s in sent] == [8, 7, 0], sent
    for qid, mtype, first, size, flg, action in sent:
        assert (qid, mtype, first, size, flg) == (3, 1, 1, 0x408, 0)
    rc, sent = emu(blob, "f870")
    assert rc == 0 and [s[5] for s in sent] == [15, 8, 7, 0]
    rc, sent = emu(blob, None)
    assert rc == 0 and sent == []
    print("cloudkick emulation OK: 8,7,0 and f->15; mtype=1; mtext[0..3]=1; size=0x408")


if __name__ == "__main__":
    main()
