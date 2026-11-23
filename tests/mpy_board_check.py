# Runs on the board (mpremote run tests/mpy_board_check.py): Wi-Fi from /wifi.json, clock from NTP, then the
# cong client checks against servers. Set TARGETS before running, or keep the defaults.
# The Wi-Fi password is read from the board's file and never printed.
import gc
import json
import sys
import time

import network
import ntptime

sys.path.append("/tests")

TARGETS = [
    # (url, auth, peer_channel)
]


def wifi():
    cfg = json.load(open("/wifi.json"))
    w = network.WLAN(network.STA_IF)
    w.active(True)
    if not w.isconnected():
        w.connect(cfg["ssid"], cfg["password"])
        for _ in range(150):
            if w.isconnected():
                break
            time.sleep_ms(100)
    del cfg
    return w.ifconfig()[0] if w.isconnected() else None


def main(targets):
    print("wifi:", wifi())
    for attempt in range(3):
        try:
            ntptime.settime()
            break
        except OSError as e:
            print("ntp retry", e)
            time.sleep(1)
    t = time.gmtime()
    print("clock (UTC): %04d-%02d-%02d %02d:%02d:%02d, epoch year %d" % (t[0], t[1], t[2], t[3], t[4], t[5],
                                                                         time.gmtime(0)[0]))
    gc.collect()
    free0 = gc.mem_free()
    import asyncio
    import mclient_check
    from iosignal import boho
    gc.collect()
    print("heap: %d free before, %d used by asyncio + client + check" % (free0, free0 - gc.mem_free()))
    print("boho clock now:", boho._now_ms())
    passed = True
    for url, auth, peer in targets:
        print("\n==", url, "login " + auth[0] if auth else "anonymous")
        gc.collect()
        before = gc.mem_free()
        passed = asyncio.run(mclient_check.main(url, auth, peer)) and passed
        gc.collect()
        print("heap free after: %d (%+d)" % (gc.mem_free(), gc.mem_free() - before))
    print("\nALL OK" if passed else "\nSOME FAILED")


main(TARGETS)
