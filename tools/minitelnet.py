#!/usr/bin/env python3
"""Minimal telnet client for the camera's debug shell.

Sends a command line, refuses every option negotiation, prints the cleaned
output.  Kept tiny because the host has no telnet/expect and Python 3.13+
removed telnetlib.
"""

from __future__ import annotations

import select
import socket
import sys
import time

IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240


def clean(buf: bytes) -> str:
    out = bytearray()
    i = 0
    while i < len(buf):
        if buf[i] == IAC and i + 1 < len(buf):
            if buf[i + 1] == SB:
                j = i + 2
                while j + 1 < len(buf) and not (buf[j] == IAC and buf[j + 1] == SE):
                    j += 1
                i = j + 2
            else:
                i += 3
        elif buf[i] == 0:
            i += 1
        else:
            out.append(buf[i])
            i += 1
    return out.decode("utf-8", "replace")


def main() -> int:
    host = sys.argv[1]
    port = int(sys.argv[2])
    command = sys.argv[3] if len(sys.argv) > 3 else "echo hello"
    settle = float(sys.argv[4]) if len(sys.argv) > 4 else 1.0
    listen = float(sys.argv[5]) if len(sys.argv) > 5 else 4.0

    s = socket.create_connection((host, port), timeout=5)
    s.setblocking(False)
    buf = bytearray()
    start = time.time()
    sent = False
    deadline = start + settle + listen

    while time.time() < deadline:
        readable, _, _ = select.select([s], [], [], 0.1)
        if readable:
            data = s.recv(65536)
            if not data:
                break
            buf += data
            reply = bytearray()
            i = 0
            while i + 2 < len(data):
                if data[i] == IAC:
                    cmd, opt = data[i + 1], data[i + 2]
                    if cmd == DO:
                        reply += bytes((IAC, WONT, opt))
                    elif cmd == WILL:
                        reply += bytes((IAC, DONT, opt))
                    i += 3
                else:
                    i += 1
            if reply:
                s.sendall(reply)
        if not sent and time.time() - start >= settle:
            s.sendall(command.encode() + b"\nexit\n")
            sent = True
    s.close()
    sys.stdout.write(clean(bytes(buf)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
