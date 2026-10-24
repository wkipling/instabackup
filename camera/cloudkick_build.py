#!/usr/bin/env python3
"""Build the AArch64 `cloudkick` helper without a cross compiler.

`instaAIP` runs a "cloud_msg" thread that does:

    q = msgget(0x44455255, IPC_CREAT|0666);
    msgrcv(q, &msg, 0x408, 1, 0);

with `msg.mtype == 1` and the action byte at mtext[4].  The same contract was
confirmed on X4 Air 1.2.7 (`cloud_rx` at Ghidra 0x14c360).  The action switch
maps:

    0  upload start   (tcp tune + ins_cloud_sdk_upload_start)
    1  upload stop
    2  upload pause
    3  upload restart
    6  capacity
    7  file group cfg (scan /tmp/SD0/DCIM, build upload list)
    8  query local uploaded files
    ...

This helper sends the action digits in argv[1] to that queue, e.g.

    ./cloudkick 70

sends action 7 (scan) followed by action 0 (start).  Messages queue up while
the scanner works, so no sleep is required.

The generated ELF is a freestanding static AArch64 executable using raw
Linux syscalls (msgget=186, msgsnd=189, exit=93).  It deliberately contains
no libc so it runs on the camera's old rootfs.
"""

import struct
import sys

BASE = 0x400000
CODE_OFF = 0x100
DATA_OFF = 0x1000
DATA_VADDR = BASE + DATA_OFF
ENTRY = BASE + CODE_OFF

# --- tiny AArch64 encoder -------------------------------------------------

def movz(rd, imm16, shift=0):
    return 0xD2800000 | ((shift // 16) << 21) | ((imm16 & 0xFFFF) << 5) | rd

def movk(rd, imm16, shift=0):
    return 0xF2800000 | ((shift // 16) << 21) | ((imm16 & 0xFFFF) << 5) | rd

def mov_reg(rd, rm):
    return 0xAA0003E0 | (rm << 16) | rd

def cmp_reg(rn, rm):
    return 0xEB00001F | (rm << 16) | (rn << 5)

def cmp_imm(rn, imm12):
    return 0xF100001F | ((imm12 & 0xFFF) << 10) | (rn << 5)

def sub_imm32(rd, rn, imm12):
    return 0x51000000 | ((imm12 & 0xFFF) << 10) | (rn << 5) | rd

def sub_imm64(rd, rn, imm12):
    return 0xD1000000 | ((imm12 & 0xFFF) << 10) | (rn << 5) | rd

def subs_imm(rd, rn, imm12):
    return 0xF1000000 | ((imm12 & 0xFFF) << 10) | (rn << 5) | rd

def add_imm(rd, rn, imm12):
    return 0x91000000 | ((imm12 & 0xFFF) << 10) | (rn << 5) | rd

def ldr_x(rt, rn, off):
    assert off % 8 == 0
    return 0xF9400000 | ((off // 8) << 10) | (rn << 5) | rt

def ldr_w(rt, rn, off):
    assert off % 4 == 0
    return 0xB9400000 | ((off // 4) << 10) | (rn << 5) | rt

def ldrb(rt, rn, off):
    return 0x39400000 | (off << 10) | (rn << 5) | rt

def ldrb_post(rt, rn, imm9=1):
    return 0x38400400 | ((imm9 & 0x1FF) << 12) | (rn << 5) | rt

def str_x(rt, rn, off):
    assert off % 8 == 0
    return 0xF9000000 | ((off // 8) << 10) | (rn << 5) | rt

def str_w(rt, rn, off):
    assert off % 4 == 0
    return 0xB9000000 | ((off // 4) << 10) | (rn << 5) | rt

def strb(rt, rn, off):
    return 0x39000000 | (off << 10) | (rn << 5) | rt

def str_x_post(rt, rn, imm9=8):
    return 0xF8000400 | ((imm9 & 0x1FF) << 12) | (rn << 5) | rt

def cbz_x(rt, label):
    return ("cbz", rt, label)

def b_cond(cond, label):
    return ("bcond", cond, label)

def b_uncond(label):
    return ("b", label)

def adr(rd, label):
    return ("adr", rd, label)

def svc():
    return 0xD4000001

COND_EQ, COND_NE, COND_LT, COND_GT, COND_LE, COND_GE = 0x0, 0x1, 0xB, 0xC, 0xD, 0xA


def assemble(items, labels):
    """items: ints or pseudo-ops. Returns bytes."""
    # first pass: sizes
    off = 0
    label_off = {}
    for it in items:
        if isinstance(it, tuple) and it[0] == "label":
            label_off[it[1]] = off
        else:
            off += 4
    words = []
    pc = 0
    for it in items:
        if isinstance(it, tuple) and it[0] == "label":
            continue
        if isinstance(it, int):
            words.append(it)
        elif it[0] == "cbz":
            target = label_off[it[2]]
            imm19 = (target - pc) // 4
            assert -(1 << 18) <= imm19 < (1 << 18)
            words.append(0xB4000000 | ((imm19 & 0x7FFFF) << 5) | it[1])
        elif it[0] == "bcond":
            target = label_off[it[2]]
            imm19 = (target - pc) // 4
            assert -(1 << 18) <= imm19 < (1 << 18)
            words.append(0x54000000 | ((imm19 & 0x7FFFF) << 5) | it[1])
        elif it[0] == "b":
            target = label_off[it[1]]
            imm26 = (target - pc) // 4
            assert -(1 << 25) <= imm26 < (1 << 25)
            words.append(0x14000000 | (imm26 & 0x3FFFFFF))
        elif it[0] == "adr":
            # adr operands may name a code label or be an absolute vaddr
            target = label_off[it[2]] if isinstance(it[2], str) else it[2]
            if isinstance(it[2], str):
                target = BASE + CODE_OFF + target
            delta = target - (BASE + CODE_OFF + pc)
            assert -(1 << 20) <= delta < (1 << 20)
            immlo = delta & 0x3
            immhi = (delta >> 2) & 0x7FFFF
            words.append(0x10000000 | (immlo << 29) | (immhi << 5) | it[1])
        else:
            raise ValueError(it)
        pc += 4
    return b"".join(struct.pack("<I", w) for w in words)


def build_code():
    # The 0x410-byte struct msgbuf lives on the stack: no RW segment and no
    # absolute data address, so the ELF is a single RX segment.
    items = [
        ("label", "start"),
        movz(0, 0x5255),                 # x0 = 0x...5255
        movk(0, 0x4445, 16),             # x0 = 0x44455255 (queue key "URED")
        movz(1, 0x3B6),                  # IPC_CREAT | 0666
        movz(8, 186),                    # msgget
        svc(),
        cmp_imm(0, 0),                   # q < 0 ?
        b_cond(COND_LT, "fail"),
        mov_reg(19, 0),                  # x19 = q
        sub_imm64(31, 31, 0x420),        # sp -= 0x420 (16-byte aligned)
        add_imm(20, 31, 0),              # x20 = sp (message buffer)
        add_imm(1, 31, 0),               # x1 = sp
        movz(2, 0x410),                  # zero 0x410 bytes
        ("label", "zero"),
        str_x_post(31, 1, 8),            # str xzr, [x1], #8
        subs_imm(2, 2, 8),
        b_cond(COND_NE, "zero"),
        movz(5, 1),
        str_x(5, 31, 0),                 # mtype = 1
        str_w(5, 31, 8),                 # mtext[0..3] = 1
        ldr_x(21, 31, 0x420),            # x21 = argc (sp restored offset)
        cmp_imm(21, 2),
        b_cond(COND_LT, "ok"),
        ldr_x(22, 31, 0x420 + 16),       # x22 = argv[1]
        ("label", "loop"),
        ldrb_post(4, 22, 1),             # w4 = *p++
        cbz_x(4, "ok"),
        movz(5, ord("0")),
        cmp_reg(4, 5),
        b_cond(COND_LT, "check_lower"),
        movz(5, ord("9")),
        cmp_reg(4, 5),
        b_cond(COND_LE, "digit"),
        ("label", "check_lower"),
        movz(5, ord("a")),
        cmp_reg(4, 5),
        b_cond(COND_LT, "check_upper"),
        movz(5, ord("f")),
        cmp_reg(4, 5),
        b_cond(COND_LE, "lower"),
        ("label", "check_upper"),
        movz(5, ord("A")),
        cmp_reg(4, 5),
        b_cond(COND_LT, "loop"),         # skip non-hex
        movz(5, ord("F")),
        cmp_reg(4, 5),
        b_cond(COND_GT, "loop"),
        sub_imm32(4, 4, ord("A") - 10),  # 'A'..'F' -> 10..15
        b_uncond("send"),
        ("label", "lower"),
        sub_imm32(4, 4, ord("a") - 10),  # 'a'..'f' -> 10..15
        b_uncond("send"),
        ("label", "digit"),
        sub_imm32(4, 4, ord("0")),       # '0'..'9' -> 0..9
        ("label", "send"),
        strb(4, 20, 12),                 # msg.mtext[4] = action
        mov_reg(0, 19),
        mov_reg(1, 20),
        movz(2, 0x408),                  # sizeof(message text)
        movz(3, 0),                      # flags
        movz(8, 189),                    # msgsnd
        svc(),
        cmp_imm(0, 0),                   # msgsnd < 0 ?
        b_cond(COND_LT, "fail"),
        b_uncond("loop"),
        ("label", "ok"),
        movz(0, 0),
        movz(8, 93),
        svc(),
        ("label", "fail"),
        movz(0, 1),
        movz(8, 93),
        svc(),
    ]
    return items


def build_elf():
    code = assemble(build_code(), None)

    buf = bytearray(CODE_OFF + len(code))
    buf[CODE_OFF:CODE_OFF + len(code)] = code

    # One RX PT_LOAD; the message buffer lives on the stack.
    e_ident = b"\x7fELF" + bytes([2, 1, 1, 0]) + b"\x00" * 8
    ehdr = e_ident + struct.pack(
        "<HHIQQQIHHHHHH",
        2,          # ET_EXEC
        183,        # EM_AARCH64
        1,          # EV_CURRENT
        ENTRY,
        64,         # e_phoff
        0,          # e_shoff
        0,          # e_flags
        64,         # e_ehsize
        56,         # e_phentsize
        1,          # e_phnum
        0,          # e_shentsize
        0,          # e_shnum
        0,          # e_shstrndx
    )
    phdr = struct.pack(
        "<IIQQQQQQ",
        1,          # PT_LOAD
        5,          # PF_R|PF_X
        0,          # p_offset
        BASE,       # p_vaddr
        BASE,       # p_paddr
        len(buf),   # p_filesz
        len(buf),   # p_memsz
        0x1000,     # p_align
    )
    out = bytearray(buf)
    out[0:64] = ehdr
    out[64:64 + 56] = phdr
    return bytes(out)


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "cloudkick"
    data = build_elf()
    with open(path, "wb") as f:
        f.write(data)
    print(f"wrote {path}: {len(data)} bytes, entry 0x{ENTRY:x}")
