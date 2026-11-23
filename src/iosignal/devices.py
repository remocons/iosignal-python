"""IOSignal device rules v2 (https://docs.iosignal.net/docs/devices/types), from iosignal devices/shared.

  find     app: #search            device: #notify <id>   (on connecting, and when asked)
  name     <id>$name   = "name" or "name:category" (split at the first ':'; no category is 미분류)
  ui       <id>$ui     = a button list "on,off,toggle", or extended "t=iris.2;p=1;v=1;b=on,off" when the
                         first key is t, p or v (t official type, p=1 $panel, v=1 $view, b buttons, broker)
  state    <id>$state  = on=1;level=70
  command  to <id>@ : a button name as it is, on, off, toggle, key=value
  group    an id starting with # has no command; the app rewrites #ch$state

A device id is its CID, and its tags are <cid>@$name, <cid>@$ui, <cid>@$state, <cid>@$panel and <cid>@$view.
The shared cases of iosignal devices/conformance/device-rules.json (tests/fixtures) check the rule functions.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, NamedTuple

from .client import IOSignal
from .protocol import Signal, State

__all__ = [
    "UNCATEGORIZED", "RAW_STATE", "Name", "Ui", "parse_name", "format_name", "is_extended_ui", "parse_ui",
    "format_ui", "parse_list", "parse_state", "format_state", "is_truthy", "render_command", "apply_command",
    "parse_object", "is_group", "Device", "DeviceInfo", "discover", "command",
]

#: The category of a device whose $name has none.
UNCATEGORIZED = "미분류"
#: Key under which :func:`parse_state` keeps the whole text.
RAW_STATE = "_"

NAME_MAX = 128  # what an app reads of $name and $ui at most (the rules recommend 64 and 128 bytes)
UI_MAX = 512

_KEY = re.compile(r"^[A-Za-z_][\w-]{0,31}$", re.ASCII)
_CHANNEL = re.compile(r"^[a-z]+\d+$")
_LINES = re.compile(r"[\r\n]+")


def is_group(id_: str) -> bool:
    return id_.startswith("#")


def _prefix(id_: str) -> str:
    return id_ + "$" if is_group(id_) else id_ + "@$"


# ---- $name

class Name(NamedTuple):
    name: str
    group: str  # the category; "" is shown as UNCATEGORIZED


def parse_name(text: Any) -> Name:
    """Split at the first ':'; both parts trimmed; line breaks become spaces."""
    value = _LINES.sub(" ", text[:NAME_MAX]) if isinstance(text, str) else ""
    at = value.find(":")
    if at < 0:
        return Name(value.strip(), "")
    return Name(value[:at].strip(), value[at + 1:].strip())


def format_name(name: str = "", group: str = "") -> str:
    """':' and line breaks in the name become spaces; no category, no ':'."""
    clean = re.sub(r"[:\r\n]", " ", str(name or "")).strip()
    category = re.sub(r"[\r\n]", " ", str(group or "")).strip()
    return f"{clean}:{category}" if category else clean


# ---- $ui

@dataclass(frozen=True)
class Ui:
    ui: str = ""  # button list (comma separated): the plain form, or b= of the extended form
    type: str = ""
    has_panel: bool = False
    has_view: bool = False
    broker: str = ""
    legacy_ui: bool = False  # the plain button list form

    @property
    def buttons(self) -> list[str]:
        return parse_list(self.ui)


def is_extended_ui(text: Any) -> bool:
    """The first ';' piece (trimmed) starts with t=, p= or v=."""
    return isinstance(text, str) and re.match(r"^(t|p|v)=", text.strip().split(";")[0]) is not None


def parse_ui(text: Any) -> Ui:
    if not isinstance(text, str) or not text.strip():
        return Ui()
    value = _LINES.sub(" ", text[:UI_MAX]).strip()
    if not is_extended_ui(value):
        return Ui(ui=value, legacy_ui=True)
    fields = parse_state(value)
    return Ui(ui=fields.get("b", ""), type=fields.get("t", ""), has_panel=fields.get("p") == "1",
              has_view=fields.get("v") == "1", broker=fields.get("broker", ""))


def format_ui(type: str = "", ui: str = "", panel: bool = False, view: bool = False, broker: str = "") -> str:  # noqa: A002
    """The plain button list, or t=;p=1;v=1;b=;broker= in that order. ';' and line breaks become spaces."""
    def clean(v: Any) -> str:
        return re.sub(r"[;\r\n]", " ", str(v or "")).strip()

    if not type and not panel and not view:
        return clean(ui)
    parts = [type and f"t={clean(type)}", panel and "p=1", view and "v=1", ui and f"b={clean(ui)}",
             broker and f"broker={clean(broker)}"]
    return ";".join(p for p in parts if p)


def parse_list(text: Any) -> list[str]:
    if isinstance(text, (list, tuple)):
        return [str(s).strip() for s in text if str(s).strip()]
    if not isinstance(text, str):
        return []
    return [s.strip() for s in text.split(",") if s.strip()]


# ---- $state and commands

def parse_state(raw: Any) -> dict[str, str]:
    """``on=1;level=70`` as a dict, with the whole text under ``"_"``. A dict is filtered to valid keys."""
    if raw is None:
        return {}
    if isinstance(raw, Mapping):
        return {k: v if isinstance(v, str) else _plain(v) for k, v in raw.items()
                if isinstance(k, str) and _KEY.match(k) and v is not None and not isinstance(v, (dict, list, tuple))}
    text = str(raw).strip()
    if not text:
        return {}
    state = {RAW_STATE: text}
    for part in text.split(";"):
        index = part.find("=")
        if index <= 0:
            continue
        key = part[:index].strip()
        if _KEY.match(key):
            state[key] = part[index + 1:].strip()
    return state


def _plain(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"  # JS String(true)
    return str(v)


def format_state(state: Mapping[str, Any]) -> str:
    return ";".join(f"{k}={_plain(v)}" for k, v in state.items() if k != RAW_STATE and _KEY.match(k))


def is_truthy(value: Any) -> bool:
    return _plain(value if value is not None else "").lower() in ("1", "on", "true")


def render_command(template: str, value: Any) -> str:
    """Widget value to a command: ``level={v}`` and 70 give ``level=70``."""
    return (template or "{v}").replace("{v}", "" if value is None else _plain(value))


def apply_command(state: Mapping[str, str], command: str) -> tuple[dict[str, str], bool]:
    """The state after a standard command, and whether it changed (JS simulate.js applyCommand).

    on/off/toggle set ``on``; ``all=0|1`` sets every channel key (lowercase + digits, 0/1 value);
    ``key=value`` sets one key; anything else is kept as ``last``.
    """
    before = {k: v for k, v in state.items() if k != RAW_STATE}
    nxt = dict(before)
    cmd = str(command if command is not None else "").strip()
    if cmd in ("on", "off"):
        nxt["on"] = "1" if cmd == "on" else "0"
    elif cmd == "toggle":
        nxt["on"] = "0" if nxt.get("on") == "1" else "1"
    elif "=" in cmd:
        key, value = (s.strip() for s in cmd.split("=", 1))
        if not _KEY.match(key) or ";" in value:
            return dict(state), False
        if key == "all":
            channels = [k for k, v in nxt.items() if _CHANNEL.match(k) and v in ("0", "1")]
            if not channels or value not in ("0", "1"):
                return dict(state), False
            for k in channels:
                nxt[k] = value
        else:
            nxt[key] = value
    else:
        nxt["last"] = cmd[:32]
    return nxt, nxt != before


def parse_object(text: Any) -> dict | None:
    """A $panel or $view value: a JSON object, or None."""
    try:
        value = json.loads(text) if isinstance(text, str) else text
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


# ---- a device on the bus

class Device:
    """A device on an :class:`IOSignal` client, following the rules as the JS virtual device runner does.

    ``on_command(command, device)`` may handle a command itself and return True; otherwise the standard
    :func:`apply_command` applies. Either way the device answers with its ``$state``, as the rules require.
    With ``channel='#lab'`` it is a group member: it answers #search with the channel, follows the shared
    ``#lab$state`` and never overwrites it on connecting. A group needs a type, panel or view.

    Create it before ``io.open()``, or on a ready client: it announces itself on every ready.
    """

    def __init__(self, io: IOSignal, name: str, group: str = "", *, type: str = "", ui: str = "",  # noqa: A002
                 panel: dict | None = None, view: dict | None = None, state: Mapping[str, Any] | None = None,
                 channel: str | None = None, on_command: Callable[[str, Device], Any] | None = None,
                 on_ready: Callable[[Device], Any] | None = None, announce_broker: bool = False):
        if channel is not None:
            if not re.match(r"^#[\w-]{1,32}$", channel):
                raise ValueError("channel must look like #lab")
            if not (type or panel or view):
                raise ValueError("a group (channel) needs a type, a panel or a view")
        parsed = parse_name(name)
        self.io = io
        self.name = parsed.name
        self.group = group or parsed.group
        self.type, self.ui, self.panel, self.view = type, ui, panel, view
        self.channel = channel
        self.state: dict[str, str] = parse_state(dict(state or {}))
        self.on_command = on_command
        self.on_ready = on_ready
        self.announce_broker = announce_broker
        self.commands: list[str] = []  # recent commands, newest last

        io.listen("#search", lambda _s: io.signal("#notify", self.id))
        io.on("@", self._command)
        if channel:
            io.listen(self._tag("state"), self._shared_state)
        io.on_event("ready", lambda _cid: self.announce())
        if io.state == State.READY:
            self.announce()

    @property
    def id(self) -> str:
        return self.channel or self.io.cid

    def _tag(self, field_: str) -> str:
        return f"{self.channel}${field_}" if self.channel else f"@${field_}"

    def announce(self) -> None:
        """$panel and $view first (p=1/v=1 point at kept values), then $name, $ui, $state, #notify."""
        io = self.io
        if self.panel:
            io.signal(self._tag("panel"), json.dumps(self.panel, ensure_ascii=False, separators=(",", ":")))
        if self.view:
            io.signal(self._tag("view"), json.dumps(self.view, ensure_ascii=False, separators=(",", ":")))
        io.signal(self._tag("name"), format_name(self.name, self.group))
        broker = io.connected_url if self.announce_broker else ""
        io.signal(self._tag("ui"), format_ui(self.type, self.ui, bool(self.panel), bool(self.view), broker))
        if not self.channel:  # a new member must not reset a group's shared state
            self.publish()
        io.signal("#notify", self.id)
        if self.on_ready:
            self._run(self.on_ready(self))

    def publish(self) -> bool:
        return self.io.state == State.READY and self.io.signal(self._tag("state"), format_state(self.state))

    def set(self, **patch: Any) -> None:
        """Changes state keys and publishes."""
        self.state.update({k: _plain(v) for k, v in patch.items()})
        self.publish()

    def _shared_state(self, signal: Signal) -> None:
        if signal.type == 1:  # TEXT
            shared = parse_state(signal.text)
            shared.pop(RAW_STATE, None)
            self.state.update(shared)

    def _command(self, signal: Signal) -> None:
        if signal.tag != "@":  # '@' also fires for every '@topic' direct signal; only '<cid>@' is a command
            return
        args = signal.args
        text = args[0] if len(args) == 1 and isinstance(args[0], str) else json.dumps(args, ensure_ascii=False)
        self.commands = (self.commands + [text])[-8:]
        handled = False
        if self.on_command is not None:
            try:
                handled = self.on_command(text, self)
            except Exception as e:  # noqa: BLE001
                self.io._emit("error", e)
            if inspect.isawaitable(handled):
                self._run(self._finish_async(handled, text))
                return
        self._finish(handled is True, text)

    async def _finish_async(self, pending: Any, text: str) -> None:
        handled = False
        try:
            handled = await pending
        except Exception as e:  # noqa: BLE001
            self.io._emit("error", e)
        self._finish(handled is True, text)

    def _finish(self, handled: bool, text: str) -> None:
        if not handled:
            self.state, _ = apply_command(self.state, text)
        self.publish()  # answer even when nothing changed, so a control drops its pending value

    def _run(self, result: Any) -> None:
        if inspect.isawaitable(result):
            task = asyncio.ensure_future(result)
            self.io._tasks.add(task)
            task.add_done_callback(self.io._task_done)


# ---- finding devices

@dataclass
class DeviceInfo:
    id: str
    name: str = ""
    group: str = ""
    ui: Ui = field(default_factory=Ui)
    ui_text: str = ""  # the $ui as sent (keys the rules ignore, such as r=1 on a MicroPython board)
    state: str = ""
    panel: dict | None = None
    view: dict | None = None

    @property
    def is_group(self) -> bool:
        return is_group(self.id)

    @property
    def category(self) -> str:
        return self.group or UNCATEGORIZED

    @property
    def fields(self) -> dict[str, str]:
        state = parse_state(self.state)
        state.pop(RAW_STATE, None)
        return state

    @property
    def shown(self) -> bool:
        """What an app lists: a name and a state (a group needs none), and no legacy2 group."""
        return bool(self.name and (self.state or self.is_group)) and not (self.is_group and self.ui.legacy_ui)


async def discover(io: IOSignal, wait: float = 1.5, panels: bool = True, views: bool = False) -> list[DeviceInfo]:
    """One search on the home channel: sends #search, follows every answer for ``wait`` seconds, then
    unsubscribes and returns what an app would list. ``io`` must be ready.
    """
    found: dict[str, DeviceInfo] = {}
    added: list[tuple[str, Callable]] = []  # (tag, handler) this search registered
    # Subscriptions the program already had stay after the search, and so do its own handlers.
    had = set(io.subscriptions)

    def follow(tag: str, handler: Callable[[Signal], None]) -> None:
        added.append((tag, handler))
        io.listen(tag, handler)

    def track(id_: str) -> None:
        if not id_ or id_ in found:
            return
        info = found[id_] = DeviceInfo(id_)
        p = _prefix(id_)

        def on_name(s: Signal) -> None:
            info.name, info.group = parse_name(s.text)

        def on_ui(s: Signal) -> None:
            info.ui = parse_ui(s.text)
            info.ui_text = s.text or ""
            followed = {t for t, _ in added}
            if panels and info.ui.has_panel and p + "panel" not in followed:
                follow(p + "panel", lambda s2: setattr(info, "panel", parse_object(s2.text)))
            if views and info.ui.has_view and p + "view" not in followed:
                follow(p + "view", lambda s2: setattr(info, "view", parse_object(s2.text)))

        def on_state(s: Signal) -> None:
            info.state = "" if s.text == "close" and not info.is_group else s.text

        follow(p + "name", on_name)
        follow(p + "ui", on_ui)
        follow(p + "state", on_state)

    notify = lambda s: track(s.text) if s.args else None  # noqa: E731
    follow("#notify", notify)
    try:
        io.signal("#search")
        await asyncio.sleep(wait)
    finally:
        for tag, handler in added:
            io.off(tag, handler)
        drop = [t for t in dict.fromkeys(t for t, _ in added) if t not in had]
        for i in range(0, len(drop), 8):
            io.unsubscribe(",".join(drop[i:i + 8]))
    return [d for d in found.values() if d.shown]


def command(io: IOSignal, device: DeviceInfo | str, cmd: str) -> bool:
    """Sends a command to a device, or writes a group's shared state as the app does."""
    info = device if isinstance(device, DeviceInfo) else DeviceInfo(device)
    if not info.is_group:
        return io.signal(info.id + "@", cmd)
    nxt, _ = apply_command(parse_state(info.state), cmd)
    return io.signal(info.id + "$state", format_state(nxt))
