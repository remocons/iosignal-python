"""Small asyncio IOSignal client over CongSocket (``cong://host:port``) for MicroPython boards.

Runs on MicroPython 1.28+ (ESP32) and on CPython 3.10+ (for testing on a computer). Plain TCP, so a
logged-in session encrypts with Boho ENC (Ascon) (no TLS needed; the board's clock must be within the
server's skew, 60 s by default, so set it with ntptime first).

    io = Client("cong://io.iosignal.net:55488", auth=("dev1", "key"))
    io.on("room#t", lambda s: print(s.text))
    io.subscribe("room#t")
    asyncio.create_task(io.run())
    await io.ready()
    io.signal("room#t", "hello")

Handlers get a :class:`iosignal.protocol.Signal`. ``signal()`` and ``subscribe()`` only queue bytes, so
they may be called from handlers and, under the lock, from another thread.
"""

import asyncio
import time

from . import cong, mbp
from .boho import Boho
from .protocol import (
    IOMsg, PayloadType, State, decode_signal, encode_payload, encode_subscription_tags, encode_tag,
    signal_packet, subscription_batches,
)

try:
    import _thread
except ImportError:
    _thread = None

try:
    _ticks_ms, _ticks_diff = time.ticks_ms, time.ticks_diff
except AttributeError:  # CPython
    def _ticks_ms():
        return int(time.monotonic() * 1000)

    def _ticks_diff(a, b):
        return a - b

PING_PERIOD_MS = 40000
PONG_TIMEOUT_MS = 15000
HELLO_TIMEOUT_MS = 15000
CALL_TIMEOUT_MS = 5000

__all__ = ["Client", "parse_url"]


def parse_url(url: str):
    """``cong://host:port`` or ``host:port`` as (host, port)."""
    if url.startswith("cong://"):
        url = url[7:]
    host, _, port = url.rstrip("/").rpartition(":")
    return host, int(port)


class Client:
    def __init__(self, url: str, auth=None, max_frame: int = 65536):
        self.url = url
        self.connected_url = url
        self.auth = auth  # (id, key) or None
        self.max_frame = max_frame
        self.state = State.CLOSED
        self.cid = ""
        self.max_message_bytes = None
        self.handlers = {}
        self.channels = []
        self.on_state = None  # function(state)
        self.on_ready = None  # function(cid)
        self.on_error = None  # function(text)
        self.tx = self.rx = 0
        self._boho = None
        self._out = []
        self._out_lock = _thread.allocate_lock() if _thread else None
        self._wake = asyncio.Event()
        self._ready = asyncio.Event()
        self._stop = False
        self._redirect = None
        self._calls = {}
        self._mid = 0
        self._last_rx = _ticks_ms()
        self._ping_at = 0

    # ---- public

    def on(self, tag: str, handler) -> None:
        encode_tag(tag, False)
        self.handlers.setdefault(tag, []).append(handler)

    def subscribe(self, tags: str) -> None:
        encoded = encode_subscription_tags(tags)
        for t in tags.split(","):
            if t not in self.channels:
                self.channels.append(t)
        if self.state == State.READY:
            self._queue(bytes([IOMsg.SUBSCRIBE, len(encoded)]) + encoded, True)

    def listen(self, tag: str, handler) -> None:
        self.on(tag, handler)
        if not tag.startswith("@"):
            self.subscribe(tag)

    def signal(self, tag: str, *values) -> bool:
        if self.state != State.READY:
            return False
        if tag.startswith("@") and self.cid:
            encode_tag(self.cid + tag)
        packet = signal_packet(tag, *encode_payload(*values))
        if self.max_message_bytes and len(packet) > self.max_message_bytes:
            return False
        self._queue(packet, True)
        return True

    def iam(self, nick: str = "") -> None:
        self._queue(bytes([IOMsg.IAM]) + nick.encode(), True)

    async def call(self, target: str, topic: str, *args, timeout_ms: int = CALL_TIMEOUT_MS):
        """RPC; returns the unpacked response (``status``, ``body``). Raises OSError on timeout or failure."""
        self._mid = (self._mid + 1) & 0xFFFF
        mid = self._mid
        waiter = [asyncio.Event(), None]
        self._calls[mid] = waiter
        self._queue(mbp.pack(mbp.u8("#MsgType", IOMsg.CALL), mbp.u16("mid", mid), mbp.string("target", target),
                             mbp.string("topic", topic), mbp.args(args)), True)
        try:
            await asyncio.wait_for(waiter[0].wait(), timeout_ms / 1000)
        except asyncio.TimeoutError:
            raise OSError("call timeout")
        finally:
            self._calls.pop(mid, None)
        res = waiter[1]
        if res is None:
            raise OSError("connection closed")
        if res.get("status", 255) >= 128:
            raise OSError("call failed: status %s %s" % (res.get("status"), res.get("body")))
        return res

    async def ready(self, timeout_ms: int = 15000) -> str:
        await asyncio.wait_for(self._ready.wait(), timeout_ms / 1000)
        return self.cid

    def stop(self) -> None:
        self._stop = True
        self._wake.set()

    async def run(self) -> None:
        """Connects, keeps the connection, and reconnects (1, 2, 4 ... 30 s) until :meth:`stop`."""
        delay = 1
        while not self._stop:
            target = self._redirect or self.url
            self._redirect = None
            try:
                await self._session(target)
                if self.state == State.READY or self._redirect:
                    delay = 1
            except Exception as e:  # noqa: BLE001
                self._error("%s: %s" % (type(e).__name__, e))
            self._drop()
            if self._stop or self._redirect:
                continue
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)

    # ---- connection

    def _set_state(self, state) -> None:
        if self.state != state:
            self.state = state
            if self.on_state:
                self.on_state(state)

    def _error(self, text: str) -> None:
        if self.on_error:
            self.on_error(text)

    def _drop(self) -> None:
        self.cid = ""
        self._ready.clear()  # the same Event: ready() callers keep waiting across redirects and reconnects
        if self._boho:
            self._boho.deauthorize()
        for waiter in self._calls.values():
            waiter[0].set()
        if self.state != State.STOP:
            self._set_state(State.STOP if self._stop else State.CLOSED)

    def _queue(self, packet: bytes, encrypt: bool) -> None:
        if encrypt and self._boho is not None and self._boho.is_authorized:
            packet = self._boho.encrypt_session(packet)
            if packet is None:
                return
        frame = cong.pack(packet)
        if self._out_lock:
            with self._out_lock:
                self._out.append(frame)
        else:
            self._out.append(frame)
        self._wake.set()

    def _take(self) -> bytes:
        if self._out_lock:
            with self._out_lock:
                out, self._out = self._out, []
        else:
            out, self._out = self._out, []
        return b"".join(out)

    async def _session(self, target: str) -> None:
        self.connected_url = target
        self._set_state(State.CONNECTING)
        host, port = parse_url(target)
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), HELLO_TIMEOUT_MS / 1000)
        self._out = []
        self._boho = None
        if self.auth:
            self._boho = Boho()
            self._boho.set_id8(self.auth[0])
            self._boho.set_key(self.auth[1])
        self._set_state(State.OPEN)
        parser = cong.Parser(self.max_frame)
        self._last_rx = opened = _ticks_ms()
        self._ping_at = 0
        hello = [False]
        tasks = [asyncio.create_task(self._writer(writer)), asyncio.create_task(self._watch(writer, opened, hello))]
        try:
            while not self._stop and not self._redirect:
                chunk = await reader.read(1024)
                if not chunk:
                    raise OSError("closed by server")
                self.rx += 1
                self._last_rx = _ticks_ms()
                self._ping_at = 0
                for frame in parser.feed(chunk):
                    if frame[:1] == bytes([IOMsg.SERVER_TIME_NONCE]):
                        hello[0] = True
                    try:
                        self._on_packet(frame)
                    except Exception as e:  # noqa: BLE001 - a bad packet or handler must not stop the loop
                        self._error("packet %s: %s" % (type(e).__name__, e))
        finally:
            for t in tasks:
                t.cancel()
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    async def _writer(self, writer) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            data = self._take()
            if data:
                writer.write(data)
                await writer.drain()
                self.tx += 1

    async def _watch(self, writer, opened: int, hello: list) -> None:
        """No hello in time, or no reply to a PING: the connection is dead. Closing it wakes the reader."""
        while True:
            await asyncio.sleep(1)
            now = _ticks_ms()
            dead = None
            if not hello[0] and _ticks_diff(now, opened) > HELLO_TIMEOUT_MS:
                dead = "no hello from the server"
            elif self._ping_at and _ticks_diff(now, self._ping_at) > PONG_TIMEOUT_MS:
                dead = "no reply to ping"
            elif not self._ping_at and _ticks_diff(now, self._last_rx) > PING_PERIOD_MS:
                self._ping_at = now
                self._queue(bytes([IOMsg.PING]), False)
            if dead:
                self._error(dead)
                writer.close()
                return

    # ---- packets

    def _on_packet(self, packet: bytes) -> None:
        if not packet:
            return
        if packet[0] == IOMsg.ENC:
            packet = self._boho.decrypt_session(packet) if self._boho else None
            if not packet:
                return
        t = packet[0]
        if t == IOMsg.SERVER_TIME_NONCE:
            self._set_state(State.SERVER_READY)
            if self._boho is None:
                self._queue(bytes([IOMsg.CID_REQ]), False)
            else:
                req = self._boho.auth_req(packet)
                if req is None:
                    self._set_state(State.AUTH_FAIL)
                else:
                    self._queue(req, False)
                    self._set_state(State.AUTH_REQ)
        elif t == IOMsg.AUTH_RES:
            if self._boho is not None and self._boho.verify_auth_res(packet):
                self._set_state(State.AUTH_RES)
                self._queue(bytes([IOMsg.CID_REQ]), False)
            else:
                self._set_state(State.AUTH_FAIL)
        elif t == IOMsg.AUTH_FAIL:
            if self._boho:
                self._boho.deauthorize()
            self._set_state(State.AUTH_FAIL)
        elif t == IOMsg.CID_RES:
            self.cid = packet[1:].decode()
            self._set_state(State.READY)
            for batch in subscription_batches(self.channels):
                encoded = encode_tag(batch)
                self._queue(bytes([IOMsg.SUBSCRIBE, len(encoded)]) + encoded, True)
            self._ready.set()
            if self.on_ready:
                self.on_ready(self.cid)
        elif t == IOMsg.SIGNAL:
            s = decode_signal(packet)
            if s is not None:
                self._dispatch(s)
        elif t == IOMsg.PING:
            self._queue(bytes([IOMsg.PONG]), False)
        elif t == IOMsg.QUOTA and len(packet) >= 5:
            self.max_message_bytes = int.from_bytes(packet[1:5], "little") or None
        elif t == IOMsg.RESPONSE_MBP:
            res = mbp.unpack(packet)
            if res is not None and res.get("mid") in self._calls:
                waiter = self._calls[res.get("mid")]
                waiter[1] = res.fields
                waiter[0].set()
        elif t == IOMsg.SERVER_REDIRECT:
            hp = cong.parse_redirect(packet)
            if hp is not None:
                self._redirect = "cong://%s:%d" % hp
            elif packet[1:8] == b"cong://":
                self._redirect = packet[1:].decode()
            else:
                self._error("redirect to a non-TCP address ignored: %s" % packet[1:].decode())
            if self._redirect:
                self._set_state(State.REDIRECTING)
        elif t == IOMsg.AUTH_CLEAR:
            self.auth = None
            self._set_state(State.AUTH_CLEAR)
            self.stop()
        elif t == IOMsg.OVER_SIZE:
            self._error("over size")

    def _dispatch(self, s) -> None:
        if s.tag.startswith("@"):
            self._invoke("@", s)
            if s.tag != "@":
                self._invoke(s.tag, s)
        else:
            self._invoke(s.tag, s)

    def _invoke(self, tag: str, s) -> None:
        for handler in self.handlers.get(tag, ()):
            try:
                handler(s)
            except Exception as e:  # noqa: BLE001
                self._error("handler %s: %s" % (tag, e))
