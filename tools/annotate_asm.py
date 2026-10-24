#!/usr/bin/env python3
"""Annotate Ghidra AArch64 asm dumps with PLT symbol names and adrp/add targets.

Ghidra addresses are expected at image base 0x100000 (readelf vaddr + 0x100000).
"""
import os
import re
import sys
import glob
from elftools.elf.elffile import ELFFile

SO = "/home/wkipling/insta360backuptool/vendor/firmware/extract/843464317/linux/usr/lib/libInsCloudSdk.so"
BASE = 0x100000
PAGE_MASK = ~0xFFF


def load():
    sections = []
    relocs = []
    plt_start = 0
    with open(SO, "rb") as fh:
        elf = ELFFile(fh)
        dynsym = elf.get_section_by_name(".dynsym")
        rela = elf.get_section_by_name(".rela.plt")
        plt_start = elf.get_section_by_name(".plt")["sh_addr"]
        for s in elf.iter_sections():
            data = b"" if s["sh_type"] == "SHT_NOBITS" else s.data()
            sections.append((s["sh_addr"], s["sh_size"], data))
        for r in rela.iter_relocations():
            relocs.append(dynsym.get_symbol(r["r_info_sym"]).name)
    return sections, relocs, plt_start


SECTIONS, RELOCS, PLT_START = load()


def plt_name(gaddr):
    off = gaddr - BASE - PLT_START
    if off < 0x20:
        return None
    idx = (off - 0x20) // 16
    if 0 <= idx < len(RELOCS):
        return RELOCS[idx]
    return None


def read_at(vaddr, n):
    for a, sz, data in SECTIONS:
        if a <= vaddr < a + sz:
            off = vaddr - a
            return data[off:off + n]
    return None


def getstr(vaddr):
    for a, sz, data in SECTIONS:
        if a <= vaddr < a + sz:
            off = vaddr - a
            end = data.find(b"\0", off)
            if end < 0 or end - off > 300:
                return None
            raw = data[off:end]
            if not raw:
                return ""
            if any(c < 32 or c > 126 for c in raw):
                return None
            return raw.decode("ascii")
    return None


def ptr_at(vaddr):
    b = read_at(vaddr, 8)
    if not b or len(b) < 8:
        return None
    import struct
    return struct.unpack("<Q", b)[0]


def main():
    asm_dir = sys.argv[1]
    out_dir = sys.argv[2] if len(sys.argv) > 2 else asm_dir
    os.makedirs(out_dir, exist_ok=True)

    for path in sorted(glob.glob(os.path.join(asm_dir, "*.asm"))):
        lines = open(path).read().splitlines()
        out = []
        for idx, line in enumerate(lines):
            if line.startswith("//"):
                out.append(line)
                continue
            m = re.match(r"^([0-9a-f]{8})\s+(.*)$", line)
            if not m:
                out.append(line)
                continue
            text = m.group(2)
            note = ""

            mb = re.match(r"bl\s+0x([0-9a-f]+)", text)
            if mb:
                tgt = int(mb.group(1), 16)
                note = plt_name(tgt) or "?"

            ma = re.match(r"adrp\s+(x\d+),0x([0-9a-f]+)", text)
            if ma:
                reg = ma.group(1)
                page = int(ma.group(2), 16)
                for j in range(1, 9):
                    if idx + j >= len(lines):
                        break
                    nxt = lines[idx + j]
                    madd = re.match(r"^[0-9a-f]{8}\s+add\s+x\d+,%s,#(0x[0-9a-f]+)" % reg, nxt)
                    if madd:
                        tgt = page + int(madd.group(1), 16)
                        s = getstr(tgt - BASE)
                        p = ptr_at(tgt - BASE)
                        ps = getstr(p) if p else None
                        if s is not None:
                            note = "adrp+add -> %06x %r" % (tgt - BASE, s)
                        elif ps is not None:
                            note = "adrp+add -> %06x ptr->%06x %r" % (tgt - BASE, p, ps)
                        else:
                            note = "adrp+add -> %06x" % (tgt - BASE)
                        break

            if note:
                out.append("%s  %-56s ; %s" % (m.group(1), text, note))
            else:
                out.append(line)

        dst = os.path.join(out_dir, os.path.basename(path).replace(".asm", ".annotated.asm"))
        open(dst, "w").write("\n".join(out) + "\n")
        print("WROTE", dst)


if __name__ == "__main__":
    main()
