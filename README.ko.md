# iosignal (Python)

[English](README.md) | [한국어](README.ko.md)

웹 앱, 장치, 보드를 잇는 실시간 시그널 브로커 [IOSignal](https://iosignal.net)의 Python 클라이언트입니다.
같은 패키지가 CPython 3.10 이상(asyncio 또는 동기)과 MicroPython 보드(ESP32, ESP32-C3, ESP32-S3)에서 돕니다.

- 시그널, 구독, RPC 호출, 서버 ping. [JS 클라이언트](https://github.com/remocons/iosignal)와 바이트 단위로 같음
- Boho 로그인과 ENC_488 암호화, 종단 간(E2E) 봉한 시그널
- WebSocket(`ws://`, `wss://`)과 TCP(`cong://`, 아두이노 보드와 같은 연결)
- [장치 규칙](https://docs.iosignal.net/docs/devices/types): IOSignal 앱에 나타나는 장치 만들기, 찾기, 명령 보내기
- MicroPython: 보드를 장치로, 봉한 원격 REPL과 파일 전송(`iosignal-repl`)

## 설치

```sh
pip install iosignal
```

의존성은 `websockets` 하나입니다.

## 동기 (스크립트)

```python
from iosignal.sync import IOSignal

with IOSignal("wss://io.iosignal.net/ws") as io:
    io.ready()                              # CID를 돌려줌
    inbox = io.listen("room#t")             # 핸들러를 안 주면 queue.Queue
    io.signal("room#t", "안녕")
    print(inbox.get(timeout=3).text)
    print(io.ping_server(), "ms")
```

핸들러는 백그라운드 스레드에서 돕니다. 핸들러 안에서 `signal()` 등은 부를 수 있지만 `ready()`, `call()`처럼 기다리는 것은 부르지 않습니다.

## asyncio

```python
import asyncio
from iosignal import IOSignal

async def main():
    io = IOSignal("wss://io.iosignal.net/ws")
    io.auth("dev1", "key")                  # 또는 io.auth("dev1.key"), 생략하면 익명
    io.listen("room#t", lambda s: print(s.tag, s.args))
    io.on_event("state", print)
    async with io:
        await io.ready()
        io.signal("room#t", "값", b"\x01")
        print(await io.ping_server(), "ms")
        await asyncio.sleep(10)

asyncio.run(main())
```

- 보내기(`signal`, `subscribe`, `unsubscribe`, `iam`)는 동기이며 연결이 없으면 False입니다.
- 핸들러는 함수나 코루틴이고 `Signal`(`tag`, `type`, `data`, `text`, `args`, `is_direct`)을 받습니다.
- 이벤트: `state`, `ready`, `message`, `direct`, `server_signal`, `iam`, `pong`, `over_size`, `error`.
- 구독은 기억했다가 재접속 때 다시 보냅니다. 로그인한 `ws://` 연결은 ENC_488로 암호화합니다.
- `await io.call(target, topic, *args)`는 서버 서비스를 부릅니다(실패하면 `RpcError`).
- `await io.ping_server(timeout=3.0)`은 서버 왕복 시간을 밀리초로 돌려줍니다. 실패하면 `PingError`, `code`는 `NOT_READY`, `BUSY`(한 번에 하나), `TIMEOUT`, `CONNECTION_CLOSED`, `INVALID_ARGUMENT`.

### 보내는 값과 페이로드 형식 (JS `signal()`과 같음)

| `signal(tag, ...)` | 형식 | 받는 쪽 `args` |
|---|---|---|
| 없음 | EMPTY | `[]` |
| `"글자"`, `42`, `3.5` | TEXT | `["글자"]`, `["42"]` |
| `b"..."` | BINARY | `[b"..."]` |
| `{...}`, `[...]`, `None` | OBJECT | `[값]` |
| 값 여러 개, bytes 없음 | MJSON | `[값, ...]` |
| 값 여러 개, bytes 있음 | MBA | `[값, ...]` |

## TCP와 E2E 시그널

- 주소가 `cong://host:port`면 TCP로 붙습니다. 로그인하면 ENC_488로 암호화하고, TCP 스위처의 redirect도 따라갑니다: `IOSignal("cong://io.iosignal.net:55488")`.
- `io.signal_e2e(tag, data, key)`는 서버가 모르는 키로 봉해 보냅니다(로그인한 연결에서만). 받은 `Signal.e2e`가 True면 `io.decrypt_e2e(sig.data, key)`로 엽니다(다른 키나 위조면 None).

## 장치

```python
from iosignal.sync import IOSignal

with IOSignal("wss://io.iosignal.net/ws") as io:
    lamp = io.device("파이썬 전등:거실", type="dimmer.1", state={"on": "0", "level": "60"})
    io.ready()
    io.run(lambda: lamp.set(level=80))      # 장치 상태는 클라이언트 스레드에서 바꿈

    for d in io.discover():                 # 같은 홈채널(같은 공인 IP)의 장치
        print(d.id, d.name, d.category, d.ui.type, d.fields)
    io.command(d, "toggle")
```

`Device`는 `#search`에 답하고 `$panel`·`$view`·`$name`·`$ui`·`$state`·`#notify` 순서로 알리며, 명령마다 `$state`로 답합니다. `on_command(cmd, device)`가 True를 돌려주면 직접 처리한 것이고, 아니면 표준 처리(`on`, `off`, `toggle`, `all=`, `key=value`)를 합니다. 예: [examples/virtual_lamp.py](examples/virtual_lamp.py), [examples/virtual_home.py](examples/virtual_home.py)(장치 다섯 개, `--minutes`로 멈출 시간).

## MicroPython 보드

### 보드에 설치

보드가 네트워크에 붙어 있으면(MicroPython 1.28 이상):

```python
import mip
mip.install("github:remocons/iosignal-python")
```

컴퓨터에서는 `mpremote mip install github:remocons/iosignal-python`. [tools/mpy_board_setup.py](tools/mpy_board_setup.py)는 USB로 미리 컴파일한 `.mpy`와 보드 설정을 넣고, [tools/mpy_wifi_setup.py](tools/mpy_wifi_setup.py)는 Wi-Fi 설정을 씁니다.

### 보드를 장치로

```python
import iosignal.board
from machine import Pin

iosignal.board.start()
led = Pin(2, Pin.OUT)
lamp = iosignal.board.device(type="light.1", state="on=0",
                             on_change=lambda d: led.value(d.get("on") == "1"))
lamp.set(on="1")   # 내 코드에서 바꿔도 앱에 바로 보임
```

- 보드 하나가 CID 하나, 장치 하나입니다. 옵션: `name`, `type`, `state`(`"on=0;level=50"`, 키 순서 유지), `buttons`, `panel`, `view`, `on_command(cmd, device)`, `on_change(device)`.
- 콜백은 링크의 타이머 안에서 돌므로 짧게 씁니다. 콜백 오류는 `board.status()`에 남고 링크는 계속 돕니다.
- 다른 장치: `board.subscribe("abc-123@$state", fn)`, `board.signal("abc-123@", "r1=1")`. 예: [examples/board_relay_buttons.py](examples/board_relay_buttons.py).
- 코드 없이: `/iosignal.json`에 `"device"` 항목(예: `{"type": "relay4.1", "pins": [16, 17, 18, 19], "invert": true}`)을 두면 핀 장치가 됩니다(light.1, dimmer.1 PWM, relay4.1, switch2.1~8.1).

### 원격 REPL `iosignal-repl`

REPL 키가 있는 보드는 어디서나 IOSignal을 거쳐 `mpremote`처럼 다룰 수 있습니다. 세션은 REPL 키와 일회용 X25519 키로 봉하므로(forward secrecy) 브로커는 봉한 바이트만 전달합니다.

```sh
export REPL_KEY_FILE=~/.iosignal/repl-myboard.key   # 또는 -k FILE
iosignal-repl find                    # 같은 네트워크의 보드
iosignal-repl repl                    # 대화형 (Ctrl-] 나가기, Ctrl-C는 보드로)
iosignal-repl exec "print(1+1)"       # 출력이 오는 대로, 예외면 종료 코드 1
iosignal-repl run app.py              # 로컬 파일을 복사하지 않고 실행
iosignal-repl ls / | cat /main.py | get /log.txt | put main.py | rm /x | mkdir /d | rmdir /d | mv /a /b
iosignal-repl info | reset
```

서버는 `-s` 또는 `IOSIGNAL_SERVER`(기본 `wss://io.iosignal.net/ws`), 보드는 `-b` 또는 `IOSIGNAL_BOARD`이고, 안 주면 같은 네트워크에 하나뿐인 보드를 씁니다. REPL 키는 [tools/mpy_repl_key.py](tools/mpy_repl_key.py)로 만듭니다.

## 지원하지 않는 것

JS의 `echo`, `link`.

## 시험

```sh
python3 -m unittest discover -s tests          # 서버 없이
micropython tests/mpy_vectors.py               # MicroPython unix 포트에서 바이트 계층
```

## 라이선스

MIT
