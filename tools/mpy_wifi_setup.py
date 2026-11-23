"""Writes Wi-Fi settings to a MicroPython board as /wifi.json, then tries them once on the board.

    .venv/bin/python tools/mpy_wifi_setup.py /dev/cu.usbserial-3110

Asks for the SSID and the password in this terminal (the password is not echoed). The values are written
through a private temporary file that is deleted right after the copy; they are never printed or logged.
The board-side check prints only whether it connected and its IP address.
"""

import getpass
import json
import os
import subprocess
import sys
import tempfile

MPREMOTE = os.path.join(os.path.dirname(sys.executable), "mpremote")

CHECK = """
import json, network, time
cfg = json.load(open('/wifi.json'))
w = network.WLAN(network.STA_IF)
w.active(True)
if not w.isconnected():
    w.connect(cfg['ssid'], cfg['password'])
    for _ in range(150):
        if w.isconnected():
            break
        time.sleep_ms(100)
print('connected' if w.isconnected() else 'NOT connected (status %s)' % w.status(),
      w.ifconfig()[0] if w.isconnected() else '')
"""


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    port = sys.argv[1]
    ssid = input("Wi-Fi SSID: ").strip()
    password = getpass.getpass("Wi-Fi password (not shown): ")
    if not ssid:
        print("no SSID")
        return 2
    fd, path = tempfile.mkstemp(prefix="wifi-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"ssid": ssid, "password": password}, f, ensure_ascii=False)
        copy = subprocess.run([MPREMOTE, "connect", port, "resume", "cp", path, ":/wifi.json"],
                              capture_output=True, text=True)
        if copy.returncode != 0:
            print("copy failed:", copy.stderr.strip().splitlines()[-1:] or copy.returncode)
            return 1
    finally:
        os.remove(path)
    print("written to the board as /wifi.json; trying it...")
    check = subprocess.run([MPREMOTE, "connect", port, "resume", "exec", CHECK], capture_output=True, text=True)
    print((check.stdout or check.stderr).strip().splitlines()[-1])
    return 0 if "connected" in check.stdout and "NOT" not in check.stdout else 1


if __name__ == "__main__":
    sys.exit(main())
