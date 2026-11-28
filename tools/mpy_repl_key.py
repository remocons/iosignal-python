"""Creates a REPL key and gives it to a MicroPython board over USB (/repl.json) and to this computer.

    .venv/bin/python tools/mpy_repl_key.py /dev/cu.usbserial-3110 ~/.iosignal/repl-mpdev5.key

The key (XXXXX-XXXXX-XXXXX-XXXXX-XXXXX, 125 random bits, src/iosignal/replkey.py) is written to the host file
with mode 0600, and its bytes as hex to the board through a private temporary file that is deleted right after
the copy. The web IDE and iosignal-repl take the text as it is. It is never printed and never sent through a server.
The board turns the remote REPL on only when /repl.json has a key.
"""

import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from iosignal import replkey  # noqa: E402

MPREMOTE = os.path.join(os.path.dirname(sys.executable), "mpremote")


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    port, host_path = sys.argv[1], os.path.expanduser(sys.argv[2])
    key_text = replkey.new_key_text()
    key_hex = replkey.key_bytes(key_text).hex()
    os.makedirs(os.path.dirname(host_path) or ".", mode=0o700, exist_ok=True)
    fd = os.open(host_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key_text + "\n")
    fd, tmp = tempfile.mkstemp(prefix="repl-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump({"key": key_hex}, f)
        copy = subprocess.run([MPREMOTE, "connect", port, "resume", "cp", tmp, ":/repl.json"],
                              capture_output=True, text=True)
    finally:
        os.remove(tmp)
    if copy.returncode != 0:
        print("copy to the board failed:", (copy.stderr.strip().splitlines() or ["?"])[-1])
        return 1
    print("REPL key written to the board (/repl.json) and to", host_path, "(mode 600)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
