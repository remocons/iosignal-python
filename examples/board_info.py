# example: kind=run; title=보드 정보; about=버전, 칩, 메모리, 플래시, Wi-Fi, IOSignal 연결을 보여 줍니다. 보드를 바꾸지 않습니다
# Prints what the board is: MicroPython version, chip, CPU clock, free memory, flash space, Wi-Fi address and
# the IOSignal link (CID, logged in or anonymous). Changes nothing on the board.
import gc
import os
import sys

import machine

gc.collect()
print("MicroPython", ".".join(str(v) for v in sys.implementation.version[:3]), "-", os.uname().machine)
print("CPU", machine.freq() // 1000000, "MHz")
print("free memory", gc.mem_free() // 1024, "kB")
fs = os.statvfs("/")
print("flash free", fs[0] * fs[3] // 1024, "of", fs[0] * fs[2] // 1024, "kB")

try:
    import network

    wlan = network.WLAN(network.STA_IF)
    print("Wi-Fi", wlan.ifconfig()[0] if wlan.isconnected() else "not connected")
except ImportError:
    print("Wi-Fi: no network module")

try:
    import iosignal.board as board

    link = board.link
    if link is None:
        print("IOSignal: not started")
    else:
        authorized = link.boho is not None and link.boho.is_authorized
        print("IOSignal CID", link.cid or "(connecting)", "- logged in" if authorized else "- anonymous")
except ImportError:
    print("IOSignal: package not installed")
