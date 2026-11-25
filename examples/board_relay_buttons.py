# example: kind=main; title=릴레이 버튼 제어기; pins=버튼 0, 1; about=버튼 두 개로 다른 relay4.1 장치를 켜고 끕니다. TARGET에 그 장치의 CID를 넣으세요
# main.py for a board (made on an ESP32-C3): buttons on GPIO 0 and 1 toggle r1 and r2 of a relay4.1
# device (set TARGET to its CID). Copy it to the board as /main.py (web IDE, or iosignal-repl put).
#
# Wiring: each button connects its pin to GND when pressed (the internal pull-up keeps it high otherwise).
# As the device rules ask of a controller, the board follows the relay's $state and sends no toggle before it
# knows the state: a press sends "r1=1" or "r1=0" (the opposite of the last state) to <relay>@.
#
# The buttons are polled every POLL_MS by a timer (board.free_timer(): Timer(0); the IOSignal link takes the
# highest one), so main.py returns at once and the REPL (USB and remote) stays free. A button counts as pressed only after it has read
# LOW for STABLE_MS in a row, and must read HIGH for STABLE_MS before it can count again: contact bounce on
# press or release, and holding the button down, give exactly one press.
import iosignal.board as board
from machine import Pin, Timer

board.start()

TARGET = "abc-123"            # the relay4.1 device's CID (shown on its card in the apps)
BUTTONS = {0: "r1", 1: "r2"}  # GPIO -> relay channel key
POLL_MS = 10
STABLE_MS = 40                # how long a level must hold to count (raise it for very noisy buttons)

state = {}  # the relay's last $state, e.g. {"r1": "0", "r2": "1", "r3": "0", "r4": "0"}; {} while unknown


class Button:
    """Debounces one pin sampled every POLL_MS: ``update()`` is True once per press."""

    def __init__(self, gpio, key):
        self.pin = Pin(gpio, Pin.IN, Pin.PULL_UP)
        self.key = key
        self.stable = 1      # debounced level (1: released)
        self.count = 0       # samples in a row that differ from it
        self.need = max(1, STABLE_MS // POLL_MS)

    def update(self):
        if self.pin.value() == self.stable:
            self.count = 0
            return False
        self.count += 1
        if self.count < self.need:
            return False
        self.stable ^= 1
        self.count = 0
        return self.stable == 0  # a new press (HIGH -> LOW held long enough)


def on_state(tag, text):
    state.clear()
    if not isinstance(text, str) or text == "close":  # "close": the relay went offline
        return
    for part in text.split(";"):
        key, _, value = part.partition("=")
        if key.strip():
            state[key.strip()] = value.strip()


def press(key):
    current = state.get(key)
    if current not in ("0", "1"):
        print("relay %s: state of %s unknown yet, not sent" % (TARGET, key))
        return
    new = "0" if current == "1" else "1"
    if board.signal(TARGET + "@", key + "=" + new):
        state[key] = new  # until the relay answers with its $state
    else:
        print("not online, %s not sent" % key)


buttons = [Button(gpio, key) for gpio, key in BUTTONS.items()]


def poll(_t):
    for b in buttons:
        if b.update():
            press(b.key)


board.subscribe(TARGET + "@$state", on_state)
# board.free_timer(): a timer the link does not use (older packages, whose link took Timer(0): 1)
timer = Timer(board.free_timer() if hasattr(board, "free_timer") else 1)
timer.init(period=POLL_MS, mode=Timer.PERIODIC, callback=poll)
# to stop polling from the REPL: timer.deinit()
