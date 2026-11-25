# iosignal (Python)

[English](README.md) | [한국어](README.ko.md)

Python client for [IOSignal](https://iosignal.net), a real-time signal broker for web apps, devices and boards.
The same package runs on CPython 3.10+ (asyncio or blocking) and on MicroPython boards (ESP32, ESP32-C3, ESP32-S3).

- Signals, subscriptions, RPC calls and server ping, byte-compatible with the [JS client](https://github.com/remocons/iosignal)
- Boho login and encryption (standard Ascon, NIST SP 800-232), end-to-end sealed signals
- WebSocket (`ws://`, `wss://`) and TCP (`cong://`, the same link Arduino boards use)
- [Device rules](https://docs.iosignal.net/docs/devices/types): make devices that appear in IOSignal apps, find and command them
- MicroPython: a board becomes a device, and a sealed remote REPL with file transfer (`iosignal-repl`)

> **0.2.0 uses Boho 3 (Ascon).** It logs in only to IOSignal servers 10.0.0 or newer; anonymous connections
> work with any server. Device keys made for 9.x servers must be issued and registered again, and boards
> need the 0.2.0 package (reinstall over USB). See [CHANGELOG.md](CHANGELOG.md).

## Install

```sh
pip install iosignal
```

The only dependency is `websockets`.

## Blocking (scripts)

```python
from iosignal.sync import IOSignal

with IOSignal("wss://io.iosignal.net/ws") as io:
    io.ready()                              # returns the CID
    inbox = io.listen("room#t")             # without a handler: a queue.Queue
    io.signal("room#t", "hello")
    print(inbox.get(timeout=3).text)
    print(io.ping_server(), "ms")
```

Handlers run on a background thread. They may send (`signal()` and so on), but not wait (`ready()`, `call()`).

## asyncio

```python
import asyncio
from iosignal import IOSignal

async def main():
    io = IOSignal("wss://io.iosignal.net/ws")
    io.auth("dev1", "key")                  # or io.auth("dev1.key"); anonymous without it
    io.listen("room#t", lambda s: print(s.tag, s.args))
    io.on_event("state", print)
    async with io:
        await io.ready()
        io.signal("room#t", "value", b"\x01")
        print(await io.ping_server(), "ms")
        await asyncio.sleep(10)

asyncio.run(main())
```

- Sending (`signal`, `subscribe`, `unsubscribe`, `iam`) is synchronous and returns False without a connection.
- Handlers are functions or coroutines and receive a `Signal` (`tag`, `type`, `data`, `text`, `args`, `is_direct`).
- Events: `state`, `ready`, `message`, `direct`, `server_signal`, `iam`, `pong`, `over_size`, `error`.
- Subscriptions are remembered and sent again after a reconnect. A logged-in `ws://` connection is encrypted with Boho (Ascon).
- `await io.call(target, topic, *args)` calls a server service (`RpcError` on failure).
- `await io.ping_server(timeout=3.0)` returns the round trip in milliseconds. It raises `PingError` with `code` `NOT_READY`, `BUSY` (one at a time), `TIMEOUT`, `CONNECTION_CLOSED` or `INVALID_ARGUMENT`.

### Values and payload types (as JS `signal()`)

| `signal(tag, ...)` | type | receiver `args` |
|---|---|---|
| nothing | EMPTY | `[]` |
| `"text"`, `42`, `3.5` | TEXT | `["text"]`, `["42"]` |
| `b"..."` | BINARY | `[b"..."]` |
| `{...}`, `[...]`, `None` | OBJECT | `[value]` |
| several values, no bytes | MJSON | `[value, ...]` |
| several values with bytes | MBA | `[value, ...]` |

## TCP and E2E signals

- A `cong://host:port` address connects over TCP. Logged in, it is encrypted with Boho (Ascon), and a TCP switcher redirect is followed: `IOSignal("cong://io.iosignal.net:55488")`.
- `io.signal_e2e(tag, data, key)` seals data with a key the server never sees (logged-in connections only). A received `Signal` with `e2e` True opens with `io.decrypt_e2e(sig.data, key)` (None for a wrong key or a forgery).

## Devices

```python
from iosignal.sync import IOSignal

with IOSignal("wss://io.iosignal.net/ws") as io:
    lamp = io.device("Python lamp:Living room", type="dimmer.1", state={"on": "0", "level": "60"})
    io.ready()
    io.run(lambda: lamp.set(level=80))      # change device state on the client's thread

    for d in io.discover():                 # devices on the same home channel (same public IP)
        print(d.id, d.name, d.category, d.ui.type, d.fields)
    io.command(d, "toggle")
```

A `Device` answers `#search`, announces `$panel`, `$view`, `$name`, `$ui`, `$state` and `#notify` in that order, and answers every command with `$state`. `on_command(cmd, device)` returning True means it handled the command; otherwise the standard commands apply (`on`, `off`, `toggle`, `all=`, `key=value`). Examples: [examples/virtual_lamp.py](examples/virtual_lamp.py), [examples/virtual_home.py](examples/virtual_home.py) (five devices, `--minutes` to stop).

## MicroPython boards

### Install on a board

With network on the board (MicroPython 1.28+):

```python
import mip
mip.install("github:remocons/iosignal-python")
```

or from a computer: `mpremote mip install github:remocons/iosignal-python`. [tools/mpy_board_setup.py](tools/mpy_board_setup.py) installs precompiled `.mpy` files over USB with the board configuration, and [tools/mpy_wifi_setup.py](tools/mpy_wifi_setup.py) writes the Wi-Fi settings.

### The board as a device

```python
import iosignal.board
from machine import Pin

iosignal.board.start()
led = Pin(2, Pin.OUT)
lamp = iosignal.board.device(type="light.1", state="on=0",
                             on_change=lambda d: led.value(d.get("on") == "1"))
lamp.set(on="1")   # changes from your own code show in the apps at once
```

- One board is one CID and one device. Options: `name`, `type`, `state` (`"on=0;level=50"`, key order kept), `buttons`, `panel`, `view`, `on_command(cmd, device)`, `on_change(device)`.
- Callbacks run in the link's timer: keep them short. Their errors go to `board.status()` and the link keeps running.
- The link runs on the chip's highest hardware timer (ESP32, ESP32-S3: `Timer(3)`; ESP32-C3: `Timer(1)`; `"timer"` in `/iosignal.json` picks another). Your code takes `Timer(board.free_timer())` (usually 0). If your code re-`init`s or `deinit`s the link's timer, the link stops silently until a reset.
- Other devices: `board.subscribe("abc-123@$state", fn)` and `board.signal("abc-123@", "r1=1")`. Example: [examples/board_relay_buttons.py](examples/board_relay_buttons.py).
- Without code: a `"device"` entry in `/iosignal.json`, for example `{"type": "relay4.1", "pins": [16, 17, 18, 19], "invert": true}`, makes a pin device (light.1, dimmer.1 PWM, relay4.1, switch2.1~8.1).
- More examples: [examples/board_iris.py](examples/board_iris.py) (an iris.2 light with four RGB LEDs; hold button 2 to show the Wi-Fi signal and connection on the LEDs), [examples/board_light.py](examples/board_light.py), and short scripts to run once: [blink.py](examples/blink.py), [button_read.py](examples/button_read.py), [board_info.py](examples/board_info.py). [board_default.py](examples/board_default.py) is the plain `main.py` (link only).
- A logged-in board without a `name` keeps the name registered for its CID. A burst of commands (a dragged slider) is answered with at most one `$state` per 300 ms, always ending with the latest state.

### Wi-Fi on small boards

- `/iosignal.json` `"txpower": 8.5` sets the Wi-Fi send power in dBm. Small ESP32-C3 boards (for example iris, C3 SuperMini) lose sent packets at full power until the connection stalls; 8.5 fixes that near the router, raise it (11, 15) in a far room. Wi-Fi power save is always off.
- `"wifi_mode": "b"` uses 802.11b only: slower, but it holds on a much weaker signal. The router must allow 802.11b.
- The link reconnects by itself when its sending stalls (no PONG to a ping, or nothing sent for 10 s), also while commands keep arriving. Every drop and reconnect is written to `/linklog.txt` with the UTC time, the reason and the signal (kept near 4 KB; `"linklog": false` turns it off).

### Remote REPL: `iosignal-repl`

A board with a REPL key can be used like `mpremote` from anywhere, through IOSignal. Sessions are sealed with the REPL key and one-time X25519 keys (forward secrecy), so the broker relays only sealed bytes.

```sh
# REPL key (64 hex digits): paste it when asked (not shown), or export REPL_KEY=<key>, -k <key>, or a key file ~/.iosignal/repl-<board>.key
iosignal-repl find                    # boards on the same network
iosignal-repl repl                    # interactive (Ctrl-] leaves, Ctrl-C goes to the board)
iosignal-repl exec "print(1+1)"       # streamed output, exit code 1 on an exception
iosignal-repl run app.py              # run a local file without copying it
iosignal-repl ls / | cat /main.py | get /log.txt | put main.py | rm /x | mkdir /d | rmdir /d | mv /a /b
iosignal-repl info | reset
```

Server: `-s` or `IOSIGNAL_SERVER` (default `wss://io.iosignal.net/ws`). Board: `-b` or `IOSIGNAL_BOARD`; without it, the only board on the network. Make a REPL key with [tools/mpy_repl_key.py](tools/mpy_repl_key.py).

## Not supported

JS `echo` and `link`.

## Tests

```sh
python3 -m unittest discover -s tests          # no server needed
micropython tests/mpy_vectors.py               # byte layer on the MicroPython unix port
```

## License

MIT
