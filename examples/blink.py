# example: kind=run; title=LED 깜박이기; pins=LED 8; about=LED를 열 번 깜박입니다. 기본은 ESP32-C3 SuperMini(GPIO 8)
# Blinks the board's LED ten times. 중지 (Ctrl-C) stops it at once.
#
# 보드별 기본 LED (LED_PIN, LED_ON):
#   ESP32-C3 SuperMini        GPIO 8,  LOW에 켜짐 (LED_ON = 0)   ← 기본값
#   ESP32 DevKit (WROOM-32)   GPIO 2,  HIGH에 켜짐 (LED_ON = 1)
#   XIAO ESP32-S3             GPIO 21, LOW에 켜짐 (LED_ON = 0)
#   XIAO ESP32-C3             사용자 LED 없음 (핀에 LED를 달아 쓰세요)
#   ESP32-C3 DevKitM-1 (GPIO 8), ESP32-S3 DevKitC-1 (GPIO 48, v1.1은 38)은 RGB LED(WS2812)라
#   켜고 끄기만으로는 안 되고 neopixel 모듈이 필요합니다.
import time

from machine import Pin

LED_PIN = 8
LED_ON = 0  # the value that lights the LED: 0 when it is wired to 3.3 V (on at LOW), 1 when to GND

led = Pin(LED_PIN, Pin.OUT, value=1 - LED_ON)  # start off
for i in range(10):
    led.value(LED_ON)
    time.sleep_ms(250)
    led.value(1 - LED_ON)
    time.sleep_ms(250)
    print("blink", i + 1)
print("done")
