"""Several virtual devices on IOSignal, one connection each, following the device rules.

    python examples/virtual_home.py wss://io.iosignal.net/ws --minutes 30
    python examples/virtual_home.py ws://127.0.0.1:19488 --category "테스트"

They appear in remocon.kr and IOSignal Desktop on the same home network (same public IP), under one
category. Devices: dimmer.1, rgb-light.1, switch4.1, fan.1 and a thermo-hygrometer.1 whose readings drift.
Stop with Ctrl+C (or after --minutes); the server then publishes close and the apps drop them.
"""

import argparse
import asyncio
import os
import random
import re
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from iosignal import IOSignal  # noqa: E402
from iosignal.devices import Device  # noqa: E402


def number_command(key: str, low: int, high: int):
    """Handles key=N within [low, high]; anything else falls through to the standard handling."""
    def handle(command: str, device: Device) -> bool:
        if not command.startswith(key + "="):
            return False
        value = command[len(key) + 1:]
        if value.lstrip("-").isdigit():
            device.state[key] = str(max(low, min(high, int(value))))
        return True  # out-of-range or bad values are refused, the state is still sent back
    return handle


def dimmer(command: str, device: Device) -> bool:
    if number_command("level", 0, 100)(command, device):
        device.state["on"] = "1" if device.state["level"] != "0" else "0"
        return True
    return False


def rgb(command: str, device: Device) -> bool:
    if command.startswith("color="):
        value = command[6:].lower()
        if re.fullmatch(r"#[0-9a-f]{6}", value):
            device.state["color"] = value
        return True
    return number_command("level", 0, 100)(command, device)


DEVICES = [
    dict(name="거실 전등", type="dimmer.1", state={"on": "0", "level": "60"}, on_command=dimmer),
    dict(name="무드등", type="rgb-light.1", state={"on": "1", "level": "40", "color": "#ff8800"}, on_command=rgb),
    dict(name="4구 스위치", type="switch4.1", state={"s1": "0", "s2": "1", "s3": "0", "s4": "0"}),
    dict(name="선풍기", type="fan.1", state={"on": "0", "speed": "3", "swing": "0"},
         on_command=number_command("speed", 1, 10)),
    dict(name="온습도계", type="thermo-hygrometer.1", state={"temp": "23.5", "hum": "48"}),
]


async def drift(sensor: Device, every: float) -> None:
    """The thermo-hygrometer reports new readings now and then."""
    temp, hum = 23.5, 48.0
    while True:
        await asyncio.sleep(every)
        temp = min(30.0, max(18.0, temp + random.uniform(-0.3, 0.3)))
        hum = min(70.0, max(30.0, hum + random.uniform(-1.0, 1.0)))
        sensor.set(temp=f"{temp:.1f}", hum=f"{hum:.0f}")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("url")
    parser.add_argument("--category", default="파이썬 가상")
    parser.add_argument("--minutes", type=float, default=0, help="stop after this long (0: until Ctrl+C)")
    parser.add_argument("--sensor-every", type=float, default=10.0, help="seconds between sensor readings")
    args = parser.parse_args()

    clients, devices = [], []
    for spec in DEVICES:
        io = IOSignal(args.url)
        name = spec["name"]
        io.on_event("ready", lambda cid, n=name, i=io: print(f"{time.strftime('%H:%M:%S')} ready  {n:8} {cid} via {i.connected_url}", flush=True))
        io.on_event("error", lambda e, n=name: print(f"{time.strftime('%H:%M:%S')} error  {n:8} {e}", flush=True))
        device = Device(io, name, args.category, **{k: v for k, v in spec.items() if k != "name"})
        clients.append(io)
        devices.append(device)

    def report(device: Device):
        last = device.commands  # Device makes a new list for every command

        def check():
            nonlocal last
            if device.commands is not last:
                last = device.commands
                print(f"{time.strftime('%H:%M:%S')} cmd    {device.name:8} {device.commands[-1]!r} -> {device.state}", flush=True)
        return check

    checks = [report(d) for d in devices]
    for io in clients:
        io.open()
    tasks = [asyncio.ensure_future(drift(devices[-1], args.sensor_every))]
    deadline = time.monotonic() + args.minutes * 60 if args.minutes else None
    try:
        while deadline is None or time.monotonic() < deadline:
            await asyncio.sleep(0.5)
            for check in checks:
                check()
    finally:
        for t in tasks:
            t.cancel()
        for io in clients:
            await io.aclose()
        print(f"{time.strftime('%H:%M:%S')} stopped", flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
