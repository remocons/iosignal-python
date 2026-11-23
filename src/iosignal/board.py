"""Starting a MicroPython board on IOSignal from its config files. ``main.py``:

    import iosignal.board
    iosignal.board.start()

Returns at once (the REPL stays free; nothing blocks the boot); Wi-Fi, clock and connection come up in the
link's timer callback, and come back by themselves when lost. Files on the board:

    /iosignal.json  {"server": "cong://io.iosignal.net:55488", "id": "mydev01", "name": "작업대 ESP32:실험실",
                     "repl": true, "device": {"type": "relay4.1", "pins": [16, 17, 18, 19], "invert": true}}
    /device.json    {"key": "<device key>"}          login key of "id" (tools/mpy_board_setup.py, never printed)
    /wifi.json      {"ssid": "...", "password": "..."}  (tools/mpy_wifi_setup.py)
    /repl.json      {"key": "<64 hex>"}               REPL key (tools/mpy_repl_key.py); no key: no remote REPL

"device" (optional) makes the board a device driving pins (pin_device): light.1 (1 pin), dimmer.1 (1 PWM pin),
relay4.1 (4 pins), switch2.1/3.1/4.1/8.1 (2/3/4/8 pins); "invert" for outputs that are on at LOW (relay
modules). A main.py that calls board.device() itself replaces it.

Without "id" the board connects anonymously (its CID changes every time; only for trying things out).
"""

import json

link = None
_name = None


def _load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def start(config="/iosignal.json"):
    """Starts the link from the config files; returns it, or None (with the reason printed) when it cannot."""
    global link, _name
    cfg = _load(config)
    if not cfg or "server" not in cfg:
        print("iosignal: no %s with a server, not started" % config)
        return None
    wifi = _load("/wifi.json")
    device = _load("/device.json") or {}
    repl = _load("/repl.json") or {}
    auth = (cfg["id"], device["key"]) if cfg.get("id") and device.get("key") else None
    if cfg.get("id") and not auth:
        print("iosignal: %s has an id but /device.json has no key, not started" % config)
        return None
    key = bytes.fromhex(repl["key"]) if cfg.get("repl", True) and repl.get("key") else None
    if not key:
        print("iosignal: no REPL key (/repl.json) or repl off: the remote REPL stays off (device only)")
    from . import replpoll
    link = replpoll.start(cfg["server"], key, auth,
                          wifi=(wifi["ssid"], wifi["password"]) if wifi else None,
                          name=cfg.get("name"))
    _name = cfg.get("name")
    del key, device, repl, wifi
    if cfg.get("device"):
        try:
            pin_device(cfg["device"])
        except Exception as e:  # noqa: BLE001 - a bad device entry must not stop the link
            print("iosignal: device not made:", e)
    print("iosignal: starting as %s on %s" % (cfg.get("id") or "anonymous", cfg["server"]))
    return link


def device(name=None, type="", state=None, buttons="", panel=None, view=None, on_command=None,  # noqa: A002
           on_change=None):
    """Makes the board a device (device rules): see iosignal.mdevice. ``name`` defaults to the config's name.

        lamp = board.device(type="light.1", state="on=0", on_change=lambda d: led.value(d.get("on") == "1"))
    """
    if link is None:
        raise RuntimeError("iosignal.board is not started (main.py: iosignal.board.start())")
    from .mdevice import Device
    d = Device(name or _name or "MicroPython", type=type, state=state, buttons=buttons, panel=panel, view=view,
               on_command=on_command, on_change=on_change)
    link.set_device(d)
    return d


# type -> state keys driven by pins (one pin per key; dimmer.1: one PWM pin for on+level)
PIN_TYPES = {
    "light.1": ("on",),
    "dimmer.1": ("on",),
    "relay4.1": ("r1", "r2", "r3", "r4"),
    "switch2.1": ("s1", "s2"),
    "switch3.1": ("s1", "s2", "s3"),
    "switch4.1": ("s1", "s2", "s3", "s4"),
    "switch8.1": ("s1", "s2", "s3", "s4", "s5", "s6", "s7", "s8"),
}


def pin_device(spec):
    """The device from an iosignal.json "device" entry: {"type", "pins", "invert"}. Outputs start off."""
    from machine import Pin
    kind = spec.get("type")
    keys = PIN_TYPES.get(kind)
    if keys is None:
        raise ValueError("type %r is not one of %s" % (kind, ", ".join(sorted(PIN_TYPES))))
    pins = [int(p) for p in spec.get("pins") or ()]
    if len(pins) != len(keys):
        raise ValueError("%s needs %d pins, got %d" % (kind, len(keys), len(pins)))
    invert = bool(spec.get("invert"))
    if kind == "dimmer.1":
        from machine import PWM
        pwm = PWM(Pin(pins[0]), freq=1000)

        def apply(d):
            try:
                level = max(0, min(100, int(float(d.get("level", "0")))))
            except ValueError:
                level = 0
            duty = level * 65535 // 100 if d.get("on") == "1" else 0
            pwm.duty_u16(65535 - duty if invert else duty)
        state = "on=0;level=50"
    else:
        outs = [Pin(p, Pin.OUT) for p in pins]

        def apply(d):
            for k, out in zip(keys, outs):
                out.value(1 if (d.get(k) == "1") != invert else 0)
        state = ";".join(k + "=0" for k in keys)
    d = device(type=kind, state=state, on_change=apply)
    apply(d)
    return d


def subscribe(tag, fn):
    """``fn(tag, value)`` for every signal on ``tag`` (e.g. another device's ``"abc-123@$state"``)."""
    if link is None:
        raise RuntimeError("iosignal.board is not started (main.py: iosignal.board.start())")
    link.subscribe(tag, fn)


def signal(tag, text=""):
    """Sends a TEXT signal, e.g. a command ``signal("abc-123@", "r1=1")``; False when not online."""
    return link is not None and link.signal(tag, text)


def status():
    """A short line about the link, for the USB REPL."""
    if link is None:
        return "not started"
    l = link
    phase = ("online" if l.ready else "logging in" if l.sock else "connecting" if l.pending
             else "waiting for Wi-Fi" if l.wlan is not None and not l.wlan.isconnected()
             else "setting the clock" if not l.clock_ok else "retrying")
    dev = l.device
    return "%s, cid %s, session %s, stats %s, recent errors %s%s" % (
        phase, l.cid or "-", l.owner or "none", l.stats, l.errors,
        ", device %s %s commands %d errors %s" % (dev.type or "-", dev.state.text(), dev.commands, dev.errors)
        if dev else "")
