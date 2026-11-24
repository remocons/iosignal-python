"""Builds the board package for the web setup page: web/pkg/*.mpy + web/pkg/manifest.json (not in git).

    python3 tools/build_web_pkg.py          # needs mpy-cross (brew micropython)

Plain bytecode (no -march): the same files load on ESP32, ESP32-C3, ESP32-S3 and others with the same .mpy
version, since no module uses native code. tools/deploy_web.sh runs this before copying web/.
"""

import hashlib
import json
import re
import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
SRC = os.path.join(ROOT, "src", "iosignal")
OUT = os.path.join(ROOT, "web", "pkg")
MODULES = ["mbp", "protocol", "ascon", "ascon_fast", "boho", "cong", "replproto", "replpoll", "board", "mdevice", "x25519"]

# Shipped as .py: ascon_fast holds viper code, which a plain-bytecode .mpy cannot carry for every board CPU.
SOURCE_MODULES = ["ascon_fast"]


def main() -> int:
    shutil.rmtree(OUT, ignore_errors=True)
    os.makedirs(OUT)
    version = subprocess.run(["mpy-cross", "--version"], capture_output=True, text=True, check=True).stdout.strip()
    with open(os.path.join(ROOT, "pyproject.toml")) as f:
        package = re.search(r'^version = "([^"]+)"', f.read(), re.M).group(1)
    files = []
    shutil.copy(os.path.join(SRC, "__init__.py"), os.path.join(OUT, "__init__.py"))
    names = ["__init__.py"]
    for m in MODULES:
        if m in SOURCE_MODULES:  # viper code: compiled on the board for its own CPU, so shipped as source
            shutil.copy(os.path.join(SRC, m + ".py"), os.path.join(OUT, m + ".py"))
            names.append(m + ".py")
            continue
        subprocess.run(["mpy-cross", "-o", os.path.join(OUT, m + ".mpy"), os.path.join(SRC, m + ".py")], check=True)
        names.append(m + ".mpy")
    whole = hashlib.sha256()
    for name in names:
        with open(os.path.join(OUT, name), "rb") as f:
            data = f.read()
        if name.endswith(".mpy") and (data[0:1] != b"M" or data[2] & 0xFC):
            raise SystemExit("%s: not a plain bytecode .mpy" % name)
        files.append({"name": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        whole.update(name.encode() + b"\0" + data)
    with open(os.path.join(OUT, names[1]), "rb") as f:
        mpy = f.read()[1]
    manifest = {
        "id": whole.hexdigest()[:16],
        "package": package,
        # Boho 3 (Ascon): a login works only with IOSignal servers of this version or newer.
        "server": "10.0.0",
        "built": time.strftime("%Y-%m-%d"),
        "mpy": mpy,
        "firmware": "1.23",
        "compiler": version,
        "files": files,
    }
    with open(os.path.join(OUT, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=1)
    print("web/pkg: %d files, %d bytes, id %s, .mpy v%d" % (len(files), sum(x["size"] for x in files), manifest["id"], mpy))
    return 0


if __name__ == "__main__":
    sys.exit(main())
