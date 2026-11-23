# Device rules: the shared cases of iosignal devices/conformance/device-rules.json (JS, Kotlin and C++
# read the same file; tests/fixtures holds a copy), plus the Device helper against the in-memory server of test_client.
import asyncio
import json
import os
import unittest

from _path import iosignal  # noqa: F401
from iosignal import devices
from iosignal.client import IOSignal
from iosignal.devices import (
    Device, apply_command, format_name, format_state, format_ui, is_extended_ui, parse_name, parse_state, parse_ui,
)
from iosignal.protocol import IOMsg, decode_signal, encode_payload, signal_packet
from test_client import FakeServer, until

RULES = os.path.join(os.path.dirname(__file__), "fixtures", "device-rules.json")
# The source, next to this repository in the development folder only.
SOURCE_RULES = os.path.join(os.path.dirname(__file__), "..", "..", "_iosignal", "devices", "conformance",
                            "device-rules.json")

# JS parseUi keys -> Ui fields
_UI_KEYS = {"ui": "ui", "type": "type", "hasPanel": "has_panel", "hasView": "has_view", "broker": "broker",
            "legacyUi": "legacy_ui"}


def ui_as_js(ui) -> dict:
    defaults = devices.Ui()
    return {js: getattr(ui, py) for js, py in _UI_KEYS.items() if getattr(ui, py) != getattr(defaults, py)}


@unittest.skipUnless(os.path.exists(SOURCE_RULES), "the iosignal source is not next to this repository")
class RulesCopyTest(unittest.TestCase):
    def test_fixture_is_current(self):
        with open(RULES, "rb") as a, open(SOURCE_RULES, "rb") as b:
            self.assertEqual(b.read(), a.read(), "copy the device rules: cp " + SOURCE_RULES + " " + RULES)


class ConformanceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(RULES, encoding="utf-8") as f:
            cls.cases = json.load(f)

    def test_name(self):
        for c in self.cases["name"]["cases"]:
            self.assertEqual((c["name"], c["group"]), tuple(parse_name(c["input"])), c["input"])
        for c in self.cases["formatName"]["cases"]:
            self.assertEqual(c["output"], format_name(**c["input"]), c["input"])

    def test_ui(self):
        for c in self.cases["ui"]["cases"]:
            self.assertEqual(c["extended"], is_extended_ui(c["input"]), c["input"])
            parsed = parse_ui(c["input"])
            self.assertEqual(c["parsed"], ui_as_js(parsed), c["input"])
            self.assertEqual(c["buttons"], parsed.buttons, c["input"])
        for c in self.cases["formatUi"]["cases"]:
            self.assertEqual(c["output"], format_ui(**c["input"]), c["input"])

    def test_state(self):
        for c in self.cases["state"]["cases"]:
            parsed = parse_state(c["input"])
            parsed.pop("_", None)
            self.assertEqual(c["fields"], parsed, c["input"])


class RulesTest(unittest.TestCase):
    def test_apply_command_follows_js(self):
        s = {"on": "0", "r1": "0", "r2": "1", "level": "70"}
        self.assertEqual(({**s, "on": "1"}, True), apply_command(s, "on"))
        self.assertEqual({**s, "on": "1"}, apply_command(s, "toggle")[0])
        self.assertEqual(({**s, "r1": "1", "r2": "1"}, True), apply_command(s, "all=1"))
        self.assertEqual((s, False), apply_command(s, "all=2"))
        self.assertEqual((s, False), apply_command(s, "bad key=1"))
        self.assertEqual((s, False), apply_command(s, "level=1;on=1"))
        self.assertEqual(({**s, "level": "30"}, True), apply_command(s, "level=30"))
        self.assertEqual(({**s, "last": "PLAY"}, True), apply_command(s, "PLAY"))
        self.assertEqual((s, False), apply_command({**s, "_": "on=0;r1=0;r2=1;level=70"}, "level=70"))  # raw text dropped
        self.assertFalse(apply_command({"s1": "1"}, "s1=1")[1])
        # all= needs channel keys with 0/1 values: none here
        self.assertEqual(({"level": "5"}, False), apply_command({"level": "5"}, "all=1"))

    def test_state_and_helpers(self):
        self.assertEqual("on=1;level=70", format_state({"_": "x", "on": 1, "level": "70", "bad key": "1"}))
        self.assertEqual({"on": "true", "n": "2"}, parse_state({"on": True, "n": 2, "obj": {}, "bad key": 1}))
        self.assertTrue(devices.is_truthy("ON") and devices.is_truthy(1) and not devices.is_truthy("0"))
        self.assertEqual("level=70", devices.render_command("level={v}", 70))
        self.assertEqual({"kind": "bulb"}, devices.parse_object('{"kind":"bulb"}'))
        self.assertIsNone(devices.parse_object("[1]"))
        self.assertIsNone(devices.parse_object("{bad"))


class BusServer(FakeServer):
    """FakeServer that also routes signals between its connections like the broker (one home channel)."""

    def __init__(self):
        super().__init__()
        self.retained: dict[str, bytes] = {}

    def subs(self, t):
        return getattr(t, "subs", set())

    def handle(self, t, data):
        super().handle(t, data)
        plain = self.received[-1][1] if self.received and self.received[-1][0] is t else data
        if plain[0] == IOMsg.SUBSCRIBE:
            tags = plain[2:2 + plain[1]].decode().split(",")
            t.subs = self.subs(t) | set(tags)
            for tag in tags:
                if tag in self.retained:
                    t.push(self.retained[tag])
        elif plain[0] == IOMsg.UNSUBSCRIBE:
            tags = plain[2:2 + plain[1]].decode().split(",")
            t.subs = self.subs(t) - set(tags)
        elif plain[0] == IOMsg.SIGNAL:
            sig = decode_signal(plain)
            body = plain[3 + plain[1]:]
            if sig.tag.startswith("@"):  # CID publish
                out_tag = t.cid + sig.tag
                dest = [c for c in self.conns if out_tag in self.subs(c)]
            elif "@" in sig.tag:  # direct
                cid, topic = sig.tag.split("@", 1)
                out_tag = "@" + topic
                dest = [c for c in self.conns if c.cid == cid]
            else:
                out_tag = sig.tag
                dest = [c for c in self.conns if out_tag in self.subs(c)]
            packet = signal_packet(out_tag, sig.type, body)
            if "$" in out_tag and not out_tag.startswith("@"):
                self.retained[out_tag] = packet
            for c in dest:
                c.push(packet)


class DeviceTest(unittest.IsolatedAsyncioTestCase):
    async def client(self, server):
        io = IOSignal("ws://bus", connect=server.connect)
        self.addAsyncCleanup(io.aclose)
        return io

    async def test_device_is_found_and_obeys(self):
        server = BusServer()
        dev_io = await self.client(server)
        lamp = Device(dev_io, "거실등:거실", type="dimmer.1", state={"on": 0, "level": 50})
        dev_io.open()
        await dev_io.ready()

        app = await self.client(server)
        app.open()
        await app.ready()
        found = await devices.discover(app, wait=0.2)
        self.assertEqual(1, len(found))
        info = found[0]
        self.assertEqual((dev_io.cid, "거실등", "거실", "dimmer.1"), (info.id, info.name, info.group, info.ui.type))
        self.assertEqual({"on": "0", "level": "50"}, info.fields)

        states = []
        app.listen(info.id + "@$state", lambda s: states.append(s.text))
        await until(lambda: states)
        devices.command(app, info, "on")
        devices.command(app, info, "level=80")
        devices.command(app, info, "unknown")
        await until(lambda: len(states) >= 4)
        self.assertEqual("on=1;level=80;last=unknown", states[-1])
        self.assertEqual({"on": "1", "level": "80", "last": "unknown"}, lamp.state)
        self.assertEqual(["on", "level=80", "unknown"], lamp.commands)

    async def test_announce_order_and_panel(self):
        server = BusServer()
        io = await self.client(server)
        panel = {"schema": 1, "title": "펌프", "controls": [{"widget": "button", "label": "A", "cmd": "a"}]}
        Device(io, "펌프", panel=panel, view={"kind": "relays", "keys": ["r1"]}, ui="r1=1,r1=0", state={"r1": "0"})
        io.open()
        await io.ready()
        await until(lambda: any(s.tag == "#notify" for s in server.signals()))
        tags = [s.tag for s in server.signals()]
        self.assertEqual(["@$panel", "@$view", "@$name", "@$ui", "@$state", "#notify"], tags)
        sent = {s.tag: s.text for s in server.signals()}
        self.assertEqual("p=1;v=1;b=r1=1,r1=0", sent["@$ui"])
        self.assertEqual(panel, json.loads(sent["@$panel"]))
        self.assertEqual("펌프", sent["@$name"])
        self.assertEqual(io.cid, sent["#notify"])

    async def test_custom_handler_and_async(self):
        server = BusServer()
        io = await self.client(server)

        async def handle(cmd, device):
            if cmd == "open":
                await asyncio.sleep(0)
                device.state["position"] = "100"
                return True
            return False

        blind = Device(io, "블라인드", type="", ui="open", state={"position": "0"}, on_command=handle)
        io.open()
        await io.ready()
        conn = next(iter(server.conns))
        conn.push(signal_packet("@", *encode_payload("open")))
        conn.push(signal_packet("@other", *encode_payload("ignored")))
        await until(lambda: blind.state.get("position") == "100")
        await until(lambda: [s.text for s in server.signals() if s.tag == "@$state"][-1] == "position=100")
        self.assertEqual(["open"], blind.commands)

    async def test_group_member(self):
        server = BusServer()
        a = await self.client(server)
        member = Device(a, "그룹 LED:실험실", type="switch2.1", channel="#lab", state={"s1": "0", "s2": "0"})
        a.open()
        await a.ready()
        await until(lambda: any(s.tag == "#notify" for s in server.signals()))
        sent = {s.tag: s.text for s in server.signals()}
        self.assertEqual("#lab", sent["#notify"])
        self.assertNotIn("#lab$state", sent)  # a member does not reset the shared state
        app = await self.client(server)
        app.open()
        await app.ready()
        devices.command(app, devices.DeviceInfo("#lab", state="s1=0;s2=0"), "s2=1")
        await until(lambda: member.state.get("s2") == "1")
        with self.assertRaises(ValueError):
            Device(a, "x", channel="#lab")  # a group needs a type, panel or view
        with self.assertRaises(ValueError):
            Device(a, "x", type="light.1", channel="lab")


if __name__ == "__main__":
    unittest.main()
