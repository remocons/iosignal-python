# example: kind=main; title=전등 장치 (light.1); pins=LED 8; about=보드가 전등 장치가 되어 앱에서 켜고 끕니다. 기본은 ESP32-C3 SuperMini(GPIO 8)
# main.py for a MicroPython board: the board starts its IOSignal link and becomes a light.1 device.
# Copy it to the board as /main.py (web IDE "main.py로 실행", iosignal-repl put, or mpremote). In apps
# (remocon.kr, IOSignal Desktop) it shows as a light with on/off; the web IDE still finds the board
# ($ui t=light.1;r=1).
#
# 보드별 기본 LED (LED_PIN, LED_ON):
#   ESP32-C3 SuperMini        GPIO 8,  LOW에 켜짐 (LED_ON = 0)   ← 기본값
#   ESP32 DevKit (WROOM-32)   GPIO 2,  HIGH에 켜짐 (LED_ON = 1)
#   XIAO ESP32-S3             GPIO 21, LOW에 켜짐 (LED_ON = 0)
#   XIAO ESP32-C3             사용자 LED 없음 (핀에 LED를 달아 쓰세요)
#   ESP32-C3 DevKitM-1 (GPIO 8), ESP32-S3 DevKitC-1 (GPIO 48, v1.1은 38)은 RGB LED(WS2812)라
#   켜고 끄기만으로는 안 되고 neopixel 모듈이 필요합니다.
import iosignal.board
from machine import Pin

LED_PIN = 8
LED_ON = 0  # the value that lights the LED: 0 when it is wired to 3.3 V (on at LOW), 1 when to GND

iosignal.board.start()

led = Pin(LED_PIN, Pin.OUT, value=1 - LED_ON)  # start off


def changed(d):
    led.value(LED_ON if d.get("on") == "1" else 1 - LED_ON)  # runs in the link's timer: keep it short


lamp = iosignal.board.device(type="light.1", state="on=0", on_change=changed)

# Your own code can change the state too; apps see it at once:
#   lamp.set(on="1")
