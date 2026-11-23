# example: kind=main; title=iris RGB 조명 (iris.2); pins=LED 4, 버튼 1·2; about=iris 보드의 RGB LED 4개를 앱에서 하나씩 색상·밝기로 제어합니다
# main.py for the iris board (ESP32-C3, four WS2812 LEDs on GPIO 4, buttons on GPIO 1 and 2): the board
# becomes an iris.2 device, the same screen and state as the Arduino example iris-rgb. In the apps
# (remocon.kr, IOSignal Desktop) the card has an on/off switch, an all-LED hue and brightness, and a hue and
# brightness for each LED.
#
# Commands (<cid>@): on, off, toggle, h=0..359 and b=0..100 (all four LEDs), h1..h4 and b1..b4 (one LED).
# Brightness 0 is off; "on" brings back each LED's last brightness.
# State: on=1;h=30;b=50;h1=30;b1=50;...  h only when the four hues are equal, b is the brightest LED.
# Wi-Fi: iris boards lose sent packets at full power until the connection stalls; set "Wi-Fi 송신 출력"
# 8.5 dBm on the USB setup page ("txpower": 8.5 in /iosignal.json). The link applies it when Wi-Fi comes up.
# Buttons, polled on Timer(1) like the relay button example (main.py returns at once, the REPL stays free):
#   button 1 (GPIO 1) turns every LED's hue by HUE_STEP degrees (each press a new color; LEDs keep their
#   differences), and lights the LEDs if they were off; button 2 (GPIO 2) turns them on or off (on release).
# Signal check without a computer: hold button 2 for 1 s. For SIGNAL_S seconds (or until a button is pressed)
#   the LEDs show the Wi-Fi signal as bars (4: -55 dBm or better, 3: -65, 2: -75, 1: weaker) in the color of
#   the connection: green = IOSignal server connected, orange = Wi-Fi only, red = no Wi-Fi. Commands from the
#   apps still apply meanwhile and show when the check ends.
import network
import time

import iosignal.board as board
from iosignal.mdevice import State
from machine import Pin, Timer
from neopixel import NeoPixel

LED_PIN = 4
HUE_BUTTON_PIN = 1
POWER_BUTTON_PIN = 2
HUE_STEP = 30  # degrees per press: 12 presses go round the color wheel
COUNT = 4
LONG_MS = 1000  # hold button 2 this long for the signal check
SIGNAL_S = 60

board.start()

leds = NeoPixel(Pin(LED_PIN, Pin.OUT), COUNT)
signal_until = 0  # ticks_ms when the signal check (hold button 2) ends; 0: not showing
hue = [30] * COUNT
level = [0] * COUNT
last = [50] * COUNT  # brought back by "on" after "off"


def hue_rgb(h, pct):
    """Full-saturation color of a hue (the wheel the app's slider draws), scaled to pct %."""
    up = (h % 60) * 255 // 60
    down = 255 - up
    r, g, b = ((255, up, 0), (down, 255, 0), (0, 255, up), (0, down, 255), (up, 0, 255), (255, 0, down))[h // 60]
    return r * pct // 100, g * pct // 100, b * pct // 100


def state_text():
    parts = ["on=%d" % (1 if any(level) else 0)]
    if hue.count(hue[0]) == COUNT:
        parts.append("h=%d" % hue[0])
    parts.append("b=%d" % max(level))
    for i in range(COUNT):
        parts.append("h%d=%d;b%d=%d" % (i + 1, hue[i], i + 1, level[i]))
    return ";".join(parts)


def show():
    if signal_until:  # the signal check owns the LEDs until it ends
        return
    for i in range(COUNT):
        leds[i] = hue_rgb(hue[i], level[i])
    leds.write()


def set_level(i, v):
    level[i] = v
    if v:
        last[i] = v


def set_all(on):
    for i in range(COUNT):
        set_level(i, (level[i] or last[i]) if on else 0)


def number(text, top):
    try:
        v = int(text)
    except ValueError:
        return None
    return v if 0 <= v <= top else None


def on_command(cmd, device):
    """Handles every iris.2 command; the board answers each one with its $state."""
    if cmd in ("on", "off"):
        set_all(cmd == "on")
    elif cmd == "toggle":
        set_all(not any(level))
    elif "=" in cmd:
        key, value = cmd.split("=", 1)
        if key == "h" and number(value, 359) is not None:
            for i in range(COUNT):
                hue[i] = number(value, 359)
        elif key == "b" and number(value, 100) is not None:
            for i in range(COUNT):
                set_level(i, number(value, 100))
        elif len(key) == 2 and key[0] in "hb" and "1" <= key[1] <= str(COUNT):
            i = int(key[1]) - 1
            v = number(value, 359 if key[0] == "h" else 100)
            if v is not None:
                if key[0] == "h":
                    hue[i] = v
                else:
                    set_level(i, v)
    update(device, publish=False)  # the link publishes $state after this
    return True


def update(device, publish=True):
    show()
    device.state = State(state_text())  # replaced whole: "h" comes and goes
    if publish:
        device.publish_state()


lamp = board.device(type="iris.2", state=state_text(), on_command=on_command)
show()


class Button:
    """Debounced on a 10 ms poll: update() gives "down" after STABLE (4 polls) of LOW, "up" after 4 of HIGH,
    and "long" once while held LONG_MS (as board_relay_buttons.py, plus the hold)."""

    def __init__(self, gpio, need=4):
        self.pin = Pin(gpio, Pin.IN, Pin.PULL_UP)
        self.stable, self.count, self.need, self.held = 1, 0, need, 0

    def update(self):
        if self.pin.value() == self.stable:
            self.count = 0
        else:
            self.count += 1
            if self.count >= self.need:
                self.stable ^= 1
                self.count = self.held = 0
                return "down" if self.stable == 0 else "up"
        if self.stable == 0:
            self.held += 1
            if self.held == LONG_MS // 10:
                return "long"
        return None


hue_button = Button(HUE_BUTTON_PIN)
power_button = Button(POWER_BUTTON_PIN)
wlan = network.WLAN(network.STA_IF)
signal = {}       # the last reading, for the REPL: signal["rssi"], signal["state"]
power_long = False
polls = 0


def rotate_hue():
    for i in range(COUNT):
        hue[i] = (hue[i] + HUE_STEP) % 360
    if not any(level):
        set_all(True)  # a color change you can see


def signal_show():
    wifi = wlan.isconnected()
    online = wifi and board.link is not None and board.link.ready
    rssi = wlan.status("rssi") if wifi else None
    bars = 0 if rssi is None else 4 if rssi >= -55 else 3 if rssi >= -65 else 2 if rssi >= -75 else 1
    color = (0, 40, 0) if online else (45, 18, 0) if wifi else (45, 0, 0)
    for i in range(COUNT):
        leds[i] = color if i < max(bars, 1) else (0, 0, 0)
    leds.write()
    signal["rssi"], signal["state"] = rssi, "online" if online else "wifi" if wifi else "no wifi"


def signal_stop():
    global signal_until
    signal_until = 0
    show()


def poll(_t):
    global signal_until, power_long, polls
    polls += 1
    h, p = hue_button.update(), power_button.update()
    if signal_until:
        if h == "down" or p == "down" or time.ticks_diff(time.ticks_ms(), signal_until) > 0:
            power_long = p == "down"  # this press only ends the check: no on/off when it is let go
            signal_stop()
        elif polls % 50 == 0:
            signal_show()
        return
    if h == "down":
        rotate_hue()
        update(lamp)
    if p == "down":
        power_long = False
    elif p == "long":
        power_long = True
        signal_until = time.ticks_add(time.ticks_ms(), SIGNAL_S * 1000)
        signal_show()
    elif p == "up" and not power_long:
        set_all(not any(level))
        update(lamp)


timer = Timer(1)
timer.init(period=10, mode=Timer.PERIODIC, callback=poll)
# lamp.command("h=120") from the REPL acts as if an app sent it
