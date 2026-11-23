"""A device on a MicroPython board (device rules v2), on the board's IOSignal link (``replpoll``).

    from iosignal import board
    from machine import Pin

    led = Pin(2, Pin.OUT)
    lamp = board.device("작업대 LED:실험실", type="light.1", state="on=0",
                        on_change=lambda d: led.value(d.get("on") == "1"))
    lamp.set(on="1")          # from your own code too: publishes $state

The board keeps one CID, so it is one device. Its ``$ui`` is ``t=<type>;...;r=1``: apps read the type and
ignore ``r``; ``r=1`` tells the web IDE and iosignal-repl that the board has the remote REPL. Without a
type the board stays ``t=micropython.1`` (not an official type, so apps show the ``b=`` buttons).

Commands (``<cid>@``): ``on_command(cmd, device)`` may handle one and return True; otherwise the standard
handling applies (on, off, toggle, ``all=0|1`` on channel keys, ``key=value``, anything else as ``last``,
like simulate.js). ``on_change(device)`` runs when the state changed. The board answers every command with
its ``$state``. Callbacks run in the link's timer callback: keep them short (set pins, no sleeps).

Runs on MicroPython and CPython (no re, typing, dataclasses; key order kept in a list).
"""

import json

BOARD_TYPE = "micropython.1"
STATE_MAX = 200
STATE_MIN_MS = 300  # at most one $state per this long: a burst (a dragged slider) gets one reply with the latest state.
# Apps send a dragged slider every 150 ms at most (iosignal/devices createThrottle, Android Widgets.kt), so a drag
# answers with about every other command; 300 ms was fine on a weak 802.11b link (2026-11-24, iris board).

try:
    from time import ticks_diff as _diff
    from time import ticks_ms as _ticks
except ImportError:  # CPython
    import time as _time

    def _ticks():
        return int(_time.monotonic() * 1000)

    def _diff(a, b):
        return a - b


def _alpha(c):
    return "a" <= c <= "z" or "A" <= c <= "Z"


def _key_ok(k):
    """^[A-Za-z_][A-Za-z0-9_-]{0,31}$ (ASCII, as the JS rules)."""
    if not k or len(k) > 32:
        return False
    if not (_alpha(k[0]) or k[0] == "_"):
        return False
    for c in k[1:]:
        if not (_alpha(c) or "0" <= c <= "9" or c in "_-"):
            return False
    return True


def _is_channel(k):
    """Lowercase letters then digits: r1, s8."""
    i = 0
    while i < len(k) and "a" <= k[i] <= "z":
        i += 1
    return 0 < i < len(k) and k[i:].isdigit()


def clean(text):
    """A value for $name/$ui/$state: ';' and line breaks become spaces."""
    return str(text).replace(";", " ").replace("\r", " ").replace("\n", " ").strip()


def format_name(name, group=""):
    name = clean(name).replace(":", " ").strip()
    group = str(group or "").replace("\r", " ").replace("\n", " ").strip()
    return name + ":" + group if group else name


def format_ui(type="", buttons="", panel=False, view=False, repl=True):  # noqa: A002
    parts = ["t=" + clean(type or BOARD_TYPE)]
    if panel:
        parts.append("p=1")
    if view:
        parts.append("v=1")
    if buttons:
        parts.append("b=" + clean(buttons))
    if repl:
        parts.append("r=1")
    return ";".join(parts)


class State:
    """``on=1;level=70`` with its key order (MicroPython dicts do not keep it)."""

    def __init__(self, init=None):
        self.keys = []
        self.values = {}
        if isinstance(init, str):
            for part in init.split(";"):
                i = part.find("=")
                key = part[:i].strip()
                if i > 0 and _key_ok(key):  # other pieces are skipped, as the rules parse $state
                    self.put(key, part[i + 1:].strip())
        elif init:
            for k in (init.keys() if hasattr(init, "keys") else ()):
                self.put(k, init[k])

    def put(self, key, value):
        """Sets a key; returns True when the state changed. Invalid keys and values are refused (ValueError)."""
        key = str(key)
        value = "true" if value is True else "false" if value is False else str(value)
        if not _key_ok(key):
            raise ValueError("bad state key: %r" % key)
        if ";" in value or "\n" in value or "\r" in value:
            raise ValueError("bad state value for %s" % key)
        if key not in self.values:
            self.keys.append(key)
        elif self.values[key] == value:
            return False
        self.values[key] = value
        return True

    def get(self, key, default=None):
        return self.values.get(key, default)

    def text(self):
        return ";".join(k + "=" + self.values[k] for k in self.keys)

    def apply(self, command):
        """The standard command handling; returns True when the state changed."""
        cmd = str(command).strip()
        if cmd in ("on", "off"):
            return self.put("on", "1" if cmd == "on" else "0")
        if cmd == "toggle":
            return self.put("on", "0" if self.values.get("on") == "1" else "1")
        if "=" in cmd:
            i = cmd.find("=")
            key, value = cmd[:i].strip(), cmd[i + 1:].strip()
            if not _key_ok(key) or ";" in value:
                return False
            if key == "all":
                channels = [k for k in self.keys if _is_channel(k) and self.values[k] in ("0", "1")]
                if not channels or value not in ("0", "1"):
                    return False
                changed = False
                for k in channels:
                    changed = self.put(k, value) or changed
                return changed
            return self.put(key, value)
        return self.put("last", cmd[:32])


class Device:
    def __init__(self, name, type="", state=None, buttons="", panel=None, view=None,  # noqa: A002
                 on_command=None, on_change=None, group=""):
        if name is None:  # no name of its own: a logged-in board keeps the name the server registered (announce)
            self.name = None
        else:
            i = name.find(":")
            self.name = format_name(name[:i], name[i + 1:]) if i >= 0 and not group else format_name(name, group)
        self.type = type
        self.buttons = buttons
        self.panel = panel
        self.view = view
        self.state = State(state)
        if len(self.state.text()) > STATE_MAX:
            raise ValueError("state text over %d bytes" % STATE_MAX)
        self.on_command = on_command
        self.on_change = on_change
        self.link = None
        self.repl = True  # the link has the remote REPL ($ui r=1); set by the link
        self.commands = 0
        self.errors = []
        self.pending = False    # a $state is owed: it goes out when STATE_MIN_MS has passed (flush)
        self._sent_at = None

    def ui(self):
        return format_ui(self.type, self.buttons, bool(self.panel), bool(self.view), self.repl)

    def get(self, key, default=None):
        return self.state.get(key, default)

    def set(self, values=None, **kv):
        """Sets keys (a dict and/or keywords) and publishes $state when something changed."""
        changed = False
        for src in (values or {}), kv:
            for k in src:
                changed = self.state.put(k, src[k]) or changed
        if changed:
            self.publish_state()
        return changed

    def publish_state(self):
        """Sends $state now, or, within STATE_MIN_MS of the last one, marks it owed: the link's timer sends the
        latest state when the time has passed (flush). Every command is still answered."""
        if self.link is None:
            return
        now = _ticks()
        if self._sent_at is not None and _diff(now, self._sent_at) < STATE_MIN_MS:
            self.pending = True
            return
        self._send_state(now)

    def flush(self):
        """Sends an owed $state once STATE_MIN_MS has passed; the link calls it every tick."""
        if self.pending and self.link is not None and _diff(_ticks(), self._sent_at) >= STATE_MIN_MS:
            self._send_state(_ticks())

    def _send_state(self, now):
        self.pending = False
        self._sent_at = now
        self.link.device_publish("@$state", self.state.text())

    def announce(self, publish, default_name=None):
        """In the rules' order: $panel, $view, $name, $ui, $state (the link adds #search and #notify).
        Without a name of its own the device sends ``default_name``, or no $name at all when that is None: a
        logged-in board then keeps the name the server stored for its CID at login."""
        if self.panel:
            publish("@$panel", json.dumps(self.panel))
        if self.view:
            publish("@$view", json.dumps(self.view))
        name = self.name or default_name
        if name:
            publish("@$name", name)
        publish("@$ui", self.ui())
        publish("@$state", self.state.text())

    def command(self, cmd):
        """A command from an app; always answered with $state."""
        self.commands += 1
        changed = False
        try:
            handled = self.on_command(cmd, self) if self.on_command else False
            if handled is not True:
                changed = self.state.apply(cmd)
            if changed and self.on_change:
                self.on_change(self)
        except Exception as e:  # noqa: BLE001 - a script error must not stop the link
            self.errors = (self.errors + ["%s: %s" % (cmd[:16], e)])[-3:]
        self.publish_state()
        return changed
