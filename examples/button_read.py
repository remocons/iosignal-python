# example: kind=run; title=버튼 읽기; pins=버튼 0; about=20초 동안 버튼을 누를 때마다 출력합니다
# Prints each press of a button for 20 seconds. GPIO 0 is the BOOT button on most ESP32 boards (ESP32-C3: 9).
# The button connects the pin to GND; the internal pull-up keeps it high otherwise. 중지 (Ctrl-C) stops it.
import time

from machine import Pin

BUTTON_PIN = 0

button = Pin(BUTTON_PIN, Pin.IN, Pin.PULL_UP)
last = button.value()
presses = 0
end = time.ticks_add(time.ticks_ms(), 20000)
print("press the button (20 s)")
while time.ticks_diff(end, time.ticks_ms()) > 0:
    now = button.value()
    if now != last:
        last = now
        if now == 0:
            presses += 1
            print("pressed", presses)
        time.sleep_ms(30)  # debounce
    time.sleep_ms(5)
print("done,", presses, "presses")
