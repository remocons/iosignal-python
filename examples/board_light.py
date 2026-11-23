# main.py for a MicroPython board: the board starts its IOSignal link and becomes a light.1 device.
# Copy it to the board as /main.py (web IDE, iosignal-repl put, or mpremote). The LED is GPIO 2 on most
# ESP32 DevKit boards. In apps (remocon.kr, IOSignal Desktop) it shows as a light with on/off; the web IDE
# still finds the board ($ui t=light.1;r=1).
import iosignal.board
from machine import Pin

iosignal.board.start()

led = Pin(2, Pin.OUT)


def changed(d):
    led.value(d.get("on") == "1")  # runs in the link's timer: keep it short


lamp = iosignal.board.device(type="light.1", state="on=0", on_change=changed)

# Your own code can change the state too; apps see it at once:
#   lamp.set(on="1")
