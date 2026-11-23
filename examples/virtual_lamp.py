"""A dimmable lamp on IOSignal, following the device rules (type dimmer.1).

    python examples/virtual_lamp.py ws://127.0.0.1:19488 --name "파이썬 전등:거실"
    python examples/virtual_lamp.py wss://example.server --auth dev1.key

It appears in remocon.kr and IOSignal Desktop on the same home network (same public IP) with the dimmer.1
screen and a bulb picture. Commands: on, off, toggle, level=0~100.
"""

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from iosignal import IOSignal  # noqa: E402
from iosignal.devices import Device  # noqa: E402


def on_command(command: str, lamp: Device) -> bool:
    if command.startswith("level="):
        value = command[6:]
        if not value.isdigit():
            return True  # refused; the device still answers with its state
        level = max(0, min(100, int(value)))
        lamp.state.update(level=str(level), on="1" if level else "0")
        return True
    return False  # on, off, toggle: the standard handling


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("url")
    parser.add_argument("--name", default="파이썬 전등:가상 장치")
    parser.add_argument("--auth", help="id.key to log in; anonymous without it")
    args = parser.parse_args()

    io = IOSignal(args.url)
    io.on_event("state", lambda s: print("state:", s.name))
    io.on_event("error", lambda e: print("error:", e))
    if args.auth:
        io.auth(args.auth)
    lamp = Device(io, args.name, type="dimmer.1", state={"on": "0", "level": "60"}, on_command=on_command)
    io.on_event("ready", lambda cid: print("ready, cid", cid))
    async with io:
        last = None
        while True:
            await asyncio.sleep(1)
            if lamp.commands and lamp.commands[-1] != last:
                last = lamp.commands[-1]
                print("command:", last, "->", lamp.state)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
