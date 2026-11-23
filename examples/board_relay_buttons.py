# main.py for a board (made on an ESP32-C3): buttons on GPIO 0 and 1 toggle r1 and r2 of a relay4.1
# device (set TARGET to its CID). Copy it to the board as /main.py (web IDE, or iosignal-repl put).
#
# Wiring: each button connects its pin to GND when pressed (the internal pull-up keeps it high otherwise).
# As the device rules ask of a controller, the board follows the relay's $state and sends no toggle before it
# knows the state: a press sends "r1=1" or "r1=0" (the opposite of the last state) to <relay>@.
# The buttons use pin interrupts, so main.py returns at once and the REPL (USB and remote) stays free.
import time

import iosignal.board as board
from machine import Pin

board.start()

TARGET = "abc-123"            # the relay4.1 device's CID (shown on its card in the apps)
BUTTONS = {0: "r1", 1: "r2"}  # GPIO -> relay channel key
DEBOUNCE_MS = 200

state = {}  # the relay's last $state, e.g. {"r1": "0", "r2": "1", "r3": "0", "r4": "0"}; {} while unknown
last_press = {}
pins = {}


def on_state(tag, text):
    state.clear()
    if not isinstance(text, str) or text == "close":  # "close": the relay went offline
        return
    for part in text.split(";"):
        key, _, value = part.partition("=")
        if key.strip():
            state[key.strip()] = value.strip()


def press(gpio, key):
    now = time.ticks_ms()
    before = last_press.get(gpio)
    if before is not None and time.ticks_diff(now, before) < DEBOUNCE_MS:
        return  # contact bounce
    last_press[gpio] = now
    if pins[gpio].value() != 0:
        return  # not held down (a bounce on release)
    current = state.get(key)
    if current not in ("0", "1"):
        print("relay %s: state of %s unknown yet, not sent" % (TARGET, key))
        return
    new = "0" if current == "1" else "1"
    if board.signal(TARGET + "@", key + "=" + new):
        state[key] = new  # until the relay answers with its $state
    else:
        print("not online, %s not sent" % key)


board.subscribe(TARGET + "@$state", on_state)
for gpio, key in BUTTONS.items():
    pins[gpio] = Pin(gpio, Pin.IN, Pin.PULL_UP)
    pins[gpio].irq(trigger=Pin.IRQ_FALLING, handler=lambda p, g=gpio, k=key: press(g, k))
