"""Sets up a MicroPython board for IOSignal over USB: the package, the config and main.py.

    .venv/bin/python tools/mpy_board_setup.py /dev/cu.usbserial-3110 --server cong://io.iosignal.net:55488 \\
        --id mydev01 --name "작업대 ESP32:실험실"
    .venv/bin/python tools/mpy_board_setup.py PORT --server cong://192.168.0.204:17488 --anonymous

- builds the board package with mpy-cross (ESP32: xtensawin) and copies it to /lib/iosignal/
- writes /iosignal.json (server, id, name, repl) and a main.py that starts iosignal.board
- asks for the device login key (not shown) and writes /device.json through a private temporary file
  that is deleted right after; the key is never printed

Wi-Fi (tools/mpy_wifi_setup.py) and the REPL key (tools/mpy_repl_key.py) have their own tools.
"""

import argparse
import getpass
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
MPREMOTE = os.path.join(os.path.dirname(sys.executable), "mpremote")
MODULES = ["mbp", "protocol", "boho", "cong", "replproto", "replpoll", "board", "mdevice", "x25519"]
MAIN = "import iosignal.board\niosignal.board.start()\n"


def mpremote(port, *args):
    r = subprocess.run([MPREMOTE, "connect", port, "resume", *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit("mpremote %s failed: %s" % (args[0], (r.stderr.strip().splitlines() or ["?"])[-1]))
    return r.stdout


def private_file(content: str, suffix: str) -> str:
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("port")
    p.add_argument("--server", required=True, help="cong://host:port")
    p.add_argument("--id", help="device login ID (the board's CID)")
    p.add_argument("--anonymous", action="store_true", help="no login (the CID changes every time)")
    p.add_argument("--name", help="$name shown in apps: 'name' or 'name:category'")
    p.add_argument("--no-repl", action="store_true", help="keep the remote REPL off")
    p.add_argument("--arch", default="xtensawin", help="mpy-cross -march (ESP32: xtensawin, ESP32-C3: rv32imc)")
    args = p.parse_args()
    if bool(args.id) == args.anonymous:
        raise SystemExit("give --id or --anonymous")

    build = tempfile.mkdtemp(prefix="iosignal-mpy-")
    files = [os.path.join(ROOT, "src", "iosignal", "__init__.py")]
    for m in MODULES:
        out = os.path.join(build, m + ".mpy")
        subprocess.run(["mpy-cross", "-march=" + args.arch, "-o", out, os.path.join(ROOT, "src", "iosignal", m + ".py")],
                       check=True)
        files.append(out)
    for d in (":lib", ":lib/iosignal"):
        subprocess.run([MPREMOTE, "connect", args.port, "resume", "mkdir", d], capture_output=True)
    mpremote(args.port, "cp", *files, ":lib/iosignal/")
    print("package: %d files in /lib/iosignal/" % len(files))

    cfg = {"server": args.server, "repl": not args.no_repl}
    if args.id:
        cfg["id"] = args.id
    if args.name:
        cfg["name"] = args.name
    temps = [private_file(json.dumps(cfg, ensure_ascii=False), ".json"), private_file(MAIN, ".py")]
    try:
        mpremote(args.port, "cp", temps[0], ":/iosignal.json")
        mpremote(args.port, "cp", temps[1], ":/main.py")
    finally:
        for t in temps:
            os.remove(t)
    print("config: /iosignal.json %s, main.py starts iosignal.board" % cfg)

    if args.id:
        key = getpass.getpass("device key for %s (not shown): " % args.id)
        if not key:
            raise SystemExit("no key")
        t = private_file(json.dumps({"key": key}), ".json")
        try:
            mpremote(args.port, "cp", t, ":/device.json")
        finally:
            os.remove(t)
        del key
        print("device key written to /device.json")
    else:
        mpremote(args.port, "exec", "import os\ntry:\n os.remove('/device.json')\nexcept OSError:\n pass")
    print("done. Wi-Fi: tools/mpy_wifi_setup.py, REPL key: tools/mpy_repl_key.py, then reset the board.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
