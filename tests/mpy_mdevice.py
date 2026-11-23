"""Checks iosignal.mdevice (the board-side device) on MicroPython and CPython:

    micropython tests/mpy_mdevice.py     (or python3; tests/test_mdevice.py also runs it)
"""
import sys

sys.path.insert(0, "src")
from iosignal import mdevice  # noqa: E402

ok = 0


def check(got, want, what):
    global ok
    if got != want:
        raise AssertionError("%s: got %r, want %r" % (what, got, want))
    ok += 1


# names and $ui
check(mdevice.format_name("작업대 LED", "실험실"), "작업대 LED:실험실", "name")
check(mdevice.format_name("a:b"), "a b", "name colon")
check(mdevice.format_ui(), "t=micropython.1;r=1", "ui board")
check(mdevice.format_ui("light.1"), "t=light.1;r=1", "ui typed")
check(mdevice.format_ui("dimmer.1", "on,off", True, True), "t=dimmer.1;p=1;v=1;b=on,off;r=1", "ui full")
check(mdevice.format_ui("", "a;b"), "t=micropython.1;b=a b;r=1", "ui clean")
check(mdevice.format_ui("relay4.1", repl=False), "t=relay4.1", "ui without REPL")
d = mdevice.Device("램프:거실", type="light.1", state="on=0")
check((d.name, d.ui()), ("램프:거실", "t=light.1;r=1"), "device name")

# state text keeps order
s = mdevice.State("on=1; level = 70;bad;9x=1;color=#ff0000")
check(s.text(), "on=1;level=70;color=#ff0000", "parse")

# commands (same answers as devices.apply_command / simulate.js)
CASES = [
    ("on=0", "on", "on=1", True), ("on=1", "on", "on=1", False), ("on=1", "off", "on=0", True),
    ("on=1", "toggle", "on=0", True), ("level=5", "toggle", "level=5;on=1", True),
    ("on=0;level=50", "level=70", "on=0;level=70", True), ("on=0", " level = 9 ", "on=0;level=9", True),
    ("r1=0;r2=1;name=x", "all=1", "r1=1;r2=1;name=x", True), ("r1=0;r2=1", "all=0", "r1=0;r2=0", True),
    ("r1=0", "all=2", "r1=0", False), ("on=1", "all=1", "on=1", False), ("s1=1;S2=0", "all=0", "s1=0;S2=0", True),
    ("on=0", "beep", "on=0;last=beep", True), ("on=0", "9x=1", "on=0", False), ("on=0", "a=b;c", "on=0", False),
    ("on=0", "x" * 40, "on=0;last=" + "x" * 32, True), ("", "=1", "", False),
]
for start, cmd, want, changed in CASES:
    st = mdevice.State(start)
    c = st.apply(cmd)
    check((st.text(), c), (want, changed), "apply %r to %r" % (cmd, start))

# device: on_command, on_change, answers every command
seen = []
sent = []


class FakeLink:
    def device_publish(self, tag, text):
        sent.append((tag, text))


def handler(cmd, dev):
    if cmd == "blink":
        seen.append("blink")
        return True
    return False


clock = [0]
mdevice._ticks = lambda: clock[0]
mdevice._diff = lambda a, b: a - b


def later(ms=1000):
    clock[0] += ms


d = mdevice.Device("x", type="dimmer.1", state="on=0;level=50", on_command=handler,
                   on_change=lambda dev: seen.append("change " + dev.state.text()))
d.link = FakeLink()
d.command("on")
later()
d.command("on")
later()
d.command("blink")
later()
check(seen, ["change on=1;level=50", "blink"], "callbacks")
check(sent, [("@$state", "on=1;level=50")] * 3, "answers")
d.set(level=80)
later()
check(sent[-1], ("@$state", "on=1;level=80"), "set")
check(d.set(level=80), False, "set same")
d.on_change = lambda dev: 1 / 0
d.command("off")
later()
check((len(d.errors), sent[-1][1]), (1, "on=0;level=80"), "error kept, still answered")

# a burst (a dragged slider): the first command answers at once, the rest share one reply with the latest state
n = len(sent)
for v in (10, 20, 30, 40):
    d.command("level=%d" % v)
    later(30)
check(len(sent) - n, 1, "burst: one reply at once")
d.flush()
check(len(sent) - n, 1, "burst: nothing more before STATE_MIN_MS")
later(mdevice.STATE_MIN_MS)
d.flush()
check((len(sent) - n, sent[-1][1]), (2, "on=0;level=40"), "burst: then one reply with the latest state")
d.flush()
check(len(sent) - n, 2, "burst: nothing owed after that")

announced = []
d2 = mdevice.Device("팬:거실", type="fan.1", state="on=0;speed=3", view={"kind": "fan"})
d2.announce(lambda tag, text: announced.append(tag))
check(announced, ["@$view", "@$name", "@$ui", "@$state"], "announce order")

# no name of its own: no $name when the link gives no default (a logged-in board keeps its registered name)
nameless = mdevice.Device(None, type="light.1", state="on=0")
sent_names = []
nameless.announce(lambda tag, text: sent_names.append((tag, text)))
check([t for t, _ in sent_names], ["@$ui", "@$state"], "nameless, logged in: no $name")
sent_names = []
nameless.announce(lambda tag, text: sent_names.append((tag, text)), "MicroPython")
check(sent_names[0], ("@$name", "MicroPython"), "nameless, anonymous: default name")

try:
    mdevice.State("on=1").put("x", "a;b")
    raise AssertionError("bad value accepted")
except ValueError:
    ok += 1
print("mdevice: %d checks ok" % ok)
