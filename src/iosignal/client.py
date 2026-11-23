"""asyncio IOSignal client over WebSocket.

Behavior follows the JS client (iosignal src/client/IOCore.js) as the Kotlin client ports it:

- Anonymous connection (CID_REQ), or Boho login with :meth:`IOSignal.auth`. A logged-in ``ws://`` session
  encrypts signals, subscriptions, IAM and calls as ENC_488 (JS ENC_MODE.AUTO); ``wss://`` relies on TLS.
- Subscriptions are remembered and sent again after every reconnect.
- A keepalive task checks the connection every 3 s: reconnects with jitter and backoff, closes a connection
  whose server never says hello or stops answering, and PINGs when nothing was received for 40 s.

Sending is synchronous: :meth:`signal`, :meth:`subscribe` and the others queue packets for the socket's
writer task and return False when nothing is connected. Call them from the event loop thread (handlers do).

Handlers may be plain functions or coroutine functions; coroutines run as tasks. A handler that raises is
reported on the ``error`` event and never stops the receive loop.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

from . import mbp
from .boho import ENC_488_META, Boho
from .protocol import (
    IOMsg, PayloadType, Signal, State, decode_signal, encode_payload, encode_subscription_tags, encode_tag,
    max_message_bytes, signal_packet, signal_size, subscription_batches, tagged,
)

__all__ = ["IOSignal", "RpcResponse", "RpcError", "AuthError", "Transport", "websocket_transport", "cong_transport",
           "default_transport", "CongTransport", "EVENTS"]

log = logging.getLogger("iosignal")

CONNECTION_CHECKER_PERIOD = 3.0
CLIENT_PING_PERIOD = 40.0
PONG_TIMEOUT = 15.0
SERVER_HELLO_TIMEOUT = 15.0
RECONNECT_JITTER = 1.0
RECONNECT_BASE = 3.0
RECONNECT_MAX = 30.0
PROMISE_TIMEOUT = 5.0
LOGIN_ID_BYTES = 8

EVENTS = ("state", "ready", "message", "direct", "server_signal", "iam", "pong", "over_size", "error")

_SENDABLE = {State.OPEN, State.SERVER_READY, State.AUTH_REQ, State.AUTH_RES, State.AUTH_FAIL, State.READY}


class Transport(Protocol):
    """One open connection: binary messages in and out."""

    async def send(self, data: bytes) -> None: ...

    async def recv(self) -> bytes | str:
        """Next message; raises when the connection ends."""

    async def close(self) -> None: ...


Connector = Callable[[str], Awaitable[Transport]]


class CongTransport:
    """TCP with CongSocket framing (``cong://host:port``), as the JS IOCongSocket and the Arduino boards."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        from .cong import Parser
        self._reader, self._writer = reader, writer
        self._parser = Parser(1 << 24)
        self._ready: list[bytes] = []

    async def send(self, data: bytes) -> None:
        from .cong import pack
        self._writer.write(pack(bytes(data)))
        await self._writer.drain()

    async def recv(self) -> bytes:
        while not self._ready:
            chunk = await self._reader.read(65536)
            if not chunk:
                raise ConnectionError("closed by the server")
            self._ready.extend(self._parser.feed(chunk))
        return self._ready.pop(0)

    async def close(self) -> None:
        self._writer.close()
        try:
            await self._writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass


async def cong_transport(url: str) -> Transport:
    host, _, port = url[len("cong://"):].rstrip("/").rpartition(":")
    if not host or not port.isdigit():
        raise ValueError("a cong URL is cong://host:port")
    reader, writer = await asyncio.open_connection(host, int(port))
    return CongTransport(reader, writer)


async def default_transport(url: str) -> Transport:
    """``cong://`` over TCP, anything else (``ws://``, ``wss://``) with the ``websockets`` package."""
    if url.lower().startswith("cong://"):
        return await cong_transport(url)
    return await websocket_transport(url)


async def websocket_transport(url: str) -> Transport:
    """Default connector, with the ``websockets`` package. Its own protocol pings are off: IOSignal PINGs."""
    from websockets.asyncio.client import connect

    return await connect(url, max_size=None, ping_interval=None, compression=None, open_timeout=None)


@dataclass(frozen=True)
class RpcResponse:
    """Reply to :meth:`IOSignal.call`. Status 0~127 is success, 128~255 failure."""

    mid: int
    status: int
    body: Any
    fields: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status < 128


class RpcError(Exception):
    """A failed call: a failure ``response`` from the service, a timeout, or a closed connection."""

    def __init__(self, message: str, response: RpcResponse | None = None):
        super().__init__(message)
        self.response = response


class PingError(Exception):
    """A failed :meth:`IOSignal.ping_server`. ``code`` is the JS ``pingServer`` error code:
    ``INVALID_ARGUMENT``, ``NOT_READY``, ``BUSY``, ``TIMEOUT`` or ``CONNECTION_CLOSED``."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class AuthError(Exception):
    """The server refused the login (AUTH_FAIL) or cleared it (AUTH_CLEAR)."""


def reconnect_delay(attempt: int, rnd: float, base: float = RECONNECT_BASE) -> float:
    """Wait before automatic attempt ``attempt`` (0: the first after a close); ``rnd`` in [0, 1)."""
    if attempt <= 0:
        return rnd * RECONNECT_JITTER
    return min(RECONNECT_MAX, base * 2 ** min(attempt - 1, 16)) * (0.8 + rnd * 0.4)


def _json_or_text(data: bytes) -> Any:
    text = data.decode(errors="replace")
    try:
        return json.loads(text)
    except ValueError:
        return text


class IOSignal:
    """IOSignal client. ``open()`` needs a running event loop.

    Events (:meth:`on_event`): ``state(State)``, ``ready(cid)``, ``message(Signal)`` for channel and
    CID-subscription signals, ``direct(Signal)`` for signals to this CID, ``server_signal(value)``,
    ``iam(value)``, ``pong()``, ``over_size()``, ``error(Exception)``.
    """

    def __init__(self, url: str | None = None, *, connect: Connector | None = None):
        self.url = url or ""
        #: Address of the current or last connection: ``url``, or where a server redirected this client.
        self.connected_url = self.url
        #: Server-assigned client ID; empty until READY.
        self.cid = ""
        self.state = State.CLOSED
        #: ID given to :meth:`auth`, or None for an anonymous connection.
        self.auth_id: str | None = None
        #: Quota level from a server before iosignal 9.0; used while no ``max_message_bytes`` came.
        self.quota_level = 3
        #: Largest message this connection may send (iosignal 9.0+ QUOTA); None until told or unlimited.
        self.max_message_bytes: int | None = None
        self._quota_told = False
        #: Reconnect while closed. :meth:`stop` clears it; :meth:`open` does not restore it, as in JS.
        self.auto_reconnect = True
        self.tx = 0
        self.rx = 0

        # Periods in seconds; tests shorten them.
        self.check_period = CONNECTION_CHECKER_PERIOD
        self.ping_period = CLIENT_PING_PERIOD
        self.pong_timeout = PONG_TIMEOUT
        self.hello_timeout = SERVER_HELLO_TIMEOUT
        self.reconnect_base = RECONNECT_BASE
        self.call_timeout = PROMISE_TIMEOUT

        self._connect_fn: Connector = connect or default_transport
        self._boho: Boho | None = None
        self._tls = False
        self._challenge: bytes | None = None
        self._channels: dict[str, None] = {}  # ordered set
        self._handlers: dict[str, list[Callable]] = {}
        self._listeners: dict[str, list[Callable]] = {name: [] for name in EVENTS}
        self._state_waiters: list[tuple[frozenset[State], asyncio.Future]] = []
        self._pending: dict[int, asyncio.Future] = {}
        self._mid = 0
        self._tasks: set[asyncio.Task] = set()

        self._gen = 0  # increases with every socket; stale socket tasks compare against it
        self._socket_task: asyncio.Task | None = None
        self._queue: asyncio.Queue[bytes] | None = None
        self._keepalive_task: asyncio.Task | None = None
        self._reconnect_attempts = 0
        self._next_connect_at = 0.0
        self._last_rx = time.monotonic()
        self._ping_sent_at = 0.0
        self._ping_server_waiting = False
        self._hello_pending_since = 0.0
        self._closed_for_good = False

    # ---- properties

    @property
    def is_authorized(self) -> bool:
        return self._boho is not None and self._boho.is_authorized

    @property
    def is_encrypted(self) -> bool:
        """True when packets go as ENC_488: logged in over ``ws://``."""
        return self.is_authorized and not self._tls

    @property
    def signal_size_limit(self) -> int:
        if self._quota_told:
            return self.max_message_bytes if self.max_message_bytes is not None else 2 ** 63
        return signal_size(self.quota_level)

    @property
    def subscriptions(self) -> list[str]:
        return list(self._channels)

    # ---- lifecycle

    def open(self, url: str | None = None) -> IOSignal:
        """Opens the connection now, replacing any current socket. Reconnect spacing starts over."""
        if self._closed_for_good:
            raise RuntimeError("IOSignal is closed")
        if url:
            self.url = url
        if not self.url:
            raise ValueError("no url")
        self._detach()
        self._reconnect_attempts = 0
        self._next_connect_at = 0.0
        self._connect(self.url)
        if self._keepalive_task is None:
            self._keepalive_task = asyncio.get_running_loop().create_task(self._keepalive_loop())
        return self

    def close(self) -> None:
        """Closes the current socket. With ``auto_reconnect``, the client reconnects later."""
        if self._socket_task is not None:
            self._schedule_reconnect()
        self._detach()
        if self.state != State.STOP:
            self._set_state(State.CLOSED)

    def stop(self) -> None:
        """Disables auto-reconnect and closes. ``open()`` can start it again."""
        self.auto_reconnect = False
        if self._keepalive_task is not None:
            self._keepalive_task.cancel()
            self._keepalive_task = None
        self._detach()
        self._set_state(State.STOP)

    async def aclose(self) -> None:
        """Stops and waits for the socket and handler tasks to end. The instance is unusable afterward."""
        task = self._socket_task
        self.stop()
        self._closed_for_good = True
        pending = [t for t in (task, *self._tasks) if t is not None and not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def __aenter__(self) -> IOSignal:
        if self.state in (State.CLOSED, State.STOP) and self.url:
            self.open()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def wait_for(self, *states: State, timeout: float | None = None) -> State:
        """Waits until the state is one of ``states`` (now or later) and returns it."""
        if self.state in states:
            return self.state
        fut = asyncio.get_running_loop().create_future()
        entry = (frozenset(states), fut)
        self._state_waiters.append(entry)
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            if entry in self._state_waiters:
                self._state_waiters.remove(entry)

    async def ready(self, timeout: float | None = 10.0) -> str:
        """Waits for READY and returns the CID. Raises AuthError on AUTH_FAIL/AUTH_CLEAR, TimeoutError on timeout."""
        state = await self.wait_for(State.READY, State.AUTH_FAIL, State.AUTH_CLEAR, timeout=timeout)
        if state != State.READY:
            raise AuthError(f"login refused ({state.name})")
        return self.cid

    # ---- login

    def auth(self, id_: str, key: str | None = None) -> IOSignal:
        """Logs in on every following connection: ``auth('id', 'key')`` or ``auth('id.key')``.

        The server makes the ID the CID. IDs longer than 8 UTF-8 bytes are cut to 8, as in Boho.
        Call before :meth:`open`, or use :meth:`login` on an open socket.
        """
        if key == "":
            key = None
        if not isinstance(id_, str) or not id_:
            raise ValueError("auth('id', 'key') or auth('id.key')")
        boho = Boho()
        if key is None:
            if not boho.set_id_key(id_) or id_.startswith(".") or id_.endswith("."):
                raise ValueError("auth('id', 'key') or auth('id.key')")
            login_id = id_.split(".", 1)[0]
        else:
            boho.set_id8(id_)
            boho.set_key(key)
            login_id = id_
        if len(login_id.encode()) > LOGIN_ID_BYTES:
            self._emit("error", ValueError(f"login ID {login_id!r} is longer than 8 bytes and is cut to them"))
        self._boho = boho
        self.auth_id = login_id
        return self

    def login(self, id_: str, key: str | None = None) -> IOSignal:
        """:meth:`auth`, then logs in on the current socket with its challenge."""
        self.auth(id_, key)
        if self._challenge is not None:
            packet = self._boho.auth_req(self._challenge)
            if packet is not None and self._send_raw(packet):
                self._set_state(State.AUTH_REQ)
        return self

    def clear_auth(self) -> None:
        """Connects anonymously from the next connection on."""
        self._boho = None
        self.auth_id = None

    # ---- receiving

    def on_event(self, name: str, handler: Callable) -> Callable:
        if name not in self._listeners:
            raise ValueError(f"unknown event {name!r}; one of {', '.join(EVENTS)}")
        self._listeners[name].append(handler)
        return handler

    def off_event(self, name: str, handler: Callable) -> None:
        if handler in self._listeners.get(name, []):
            self._listeners[name].remove(handler)

    def on(self, tag: str, handler: Callable[[Signal], Any]) -> Callable:
        """Registers a handler for an exact tag without subscribing. ``@`` receives every direct signal."""
        encode_tag(tag, allow_empty=False)
        self._handlers.setdefault(tag, []).append(handler)
        return handler

    def off(self, tag: str, handler: Callable | None = None) -> None:
        """Removes one handler, or all handlers of ``tag``."""
        handlers = self._handlers.get(tag)
        if not handlers:
            return
        if handler is None:
            handlers.clear()
        elif handler in handlers:
            handlers.remove(handler)
        if not handlers:
            del self._handlers[tag]

    def listen(self, tag: str, handler: Callable[[Signal], Any]) -> Callable:
        """:meth:`on` plus :meth:`subscribe` for channel and CID-subscription tags."""
        if not tag.startswith("@"):
            encode_subscription_tags(tag)
        self.on(tag, handler)
        if not tag.startswith("@"):
            self.subscribe(tag)
        return handler

    def subscribe(self, tags: str) -> bool:
        """Subscribes to exact tags (comma separated, no spaces). Remembered for reconnects."""
        encoded = encode_subscription_tags(tags)
        if not encoded:
            raise ValueError("subscription tag is empty")
        for t in tags.split(","):
            self._channels[t] = None
        if self.state == State.READY:
            return self._send_enc(tagged(IOMsg.SUBSCRIBE, encoded))
        return False

    def unsubscribe(self, tags: str) -> bool:
        encoded = encode_subscription_tags(tags)
        if not encoded:
            raise ValueError("use unsubscribe_all() to remove every subscription")
        for t in tags.split(","):
            self._channels.pop(t, None)
        return self._send_enc(tagged(IOMsg.UNSUBSCRIBE, encoded))

    def unsubscribe_all(self) -> bool:
        self._channels.clear()
        return self._send_enc(tagged(IOMsg.UNSUBSCRIBE, b""))

    # ---- sending

    def signal(self, tag: str, *values: Any) -> bool:
        """Sends a signal; the payload type follows JS ``signal(tag, ...args)``.

        none: EMPTY; str/int/float: TEXT; bytes: BINARY; dict/list/None: OBJECT; several: MBA if any is
        bytes, else MJSON. Returns False when it could not be queued (not connected, or over size).
        """
        if tag.startswith("@") and self.cid:
            encode_tag(self.cid + tag)  # the server prefixes the sender CID; that tag must still fit
        return self._send_enc(signal_packet(tag, *encode_payload(*values)))

    def iam(self, nick: str | None = None) -> bool:
        """Requests connection info (event ``iam``). A nick of 1~28 bytes renames this connection."""
        return self._send_enc(bytes([IOMsg.IAM]) + (nick.encode() if nick else b""))

    def ping(self) -> bool:
        sent = self._send_raw(bytes([IOMsg.PING]))
        if sent and not self._ping_sent_at:
            self._ping_sent_at = time.monotonic()
        return sent

    async def ping_server(self, timeout: float = 3.0) -> float:
        """Measures the round trip to the server: sends a PING and returns the milliseconds until its PONG
        (JS ``pingServer({timeoutMs})``, with the timeout in seconds). One measurement at a time.

        Raises PingError with ``code`` NOT_READY (the connection is not ready), BUSY (already measuring),
        TIMEOUT (no PONG in ``timeout`` seconds), CONNECTION_CLOSED (closed before the PONG) or
        INVALID_ARGUMENT (``timeout`` outside 0.001~60).
        """
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0.001 <= timeout <= 60:
            raise PingError("INVALID_ARGUMENT", "timeout must be a number of seconds from 0.001 to 60")
        if self.state != State.READY:
            raise PingError("NOT_READY", "the server connection is not ready")
        if self._ping_server_waiting:
            raise PingError("BUSY", "already waiting for a pong")
        self._ping_server_waiting = True
        fut = asyncio.get_running_loop().create_future()

        def on_pong() -> None:
            if not fut.done():
                fut.set_result(None)

        def on_state(state: State) -> None:
            if state != State.READY and not fut.done():
                fut.set_exception(PingError("CONNECTION_CLOSED", "the connection closed before the pong"))

        self.on_event("pong", on_pong)
        self.on_event("state", on_state)
        started = time.perf_counter()
        try:
            if not self.ping():
                raise PingError("CONNECTION_CLOSED", "the ping could not be sent")
            await asyncio.wait_for(asyncio.shield(fut), timeout)
            return (time.perf_counter() - started) * 1000.0
        except asyncio.TimeoutError:
            raise PingError("TIMEOUT", f"no pong in {timeout:g} s") from None
        finally:
            self.off_event("pong", on_pong)
            self.off_event("state", on_state)
            self._ping_server_waiting = False
            if not fut.done():
                fut.cancel()

    async def call(self, target: str, topic: str, *args: Any, timeout: float | None = None) -> RpcResponse:
        """Remote service call (CALL / RESPONSE_MBP), JS ``call(target, topic, ...args)``.

        Raises RpcError for a failure status, a timeout, a closed connection, or a call that cannot be sent.
        """
        if not target or not topic:
            raise ValueError("call needs a target and a topic")
        arg_fields = mbp.args(args)
        self._mid = (self._mid + 1) & 0xFFFF
        mid = self._mid
        old = self._pending.pop(mid, None)
        if old is not None and not old.done():
            old.set_exception(RpcError("message ID reused"))
        fut = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        packet = mbp.pack(mbp.u8("#MsgType", IOMsg.CALL), mbp.u16("mid", mid), mbp.string("target", target),
                          mbp.string("topic", topic), arg_fields)
        if not self._send_enc(packet):
            self._pending.pop(mid, None)
            raise RpcError("not connected")
        try:
            return await asyncio.wait_for(asyncio.shield(fut), timeout if timeout is not None else self.call_timeout)
        except asyncio.TimeoutError:
            raise RpcError("timeout") from None
        finally:
            if self._pending.get(mid) is fut:
                del self._pending[mid]

    def signal_e2e(self, tag: str, data: bytes | str, key: str | bytes) -> bool:
        """Sends ``data`` sealed with an E2E key the server never sees (JS ``signal_e2e``): only a receiver with
        the key opens it, with :meth:`decrypt_e2e`. As in JS, only from a logged-in connection."""
        if not self.is_authorized:
            return False
        tag_bytes = encode_tag(tag)
        if tag.startswith("@") and self.cid:
            encode_tag(self.cid + tag)
        secret = self._boho.encrypt_e2e(data.encode() if isinstance(data, str) else bytes(data), key)
        header = bytes([IOMsg.SIGNAL_E2E, len(tag_bytes)]) + tag_bytes + bytes([PayloadType.BINARY])
        if not self.is_encrypted:
            return self._send_raw(header + secret)
        enc = self._boho.encrypt_488(header)  # the header only: the payload is sealed already
        if enc is None:
            return False
        return self._send_raw(bytes([IOMsg.ENC_E2E]) + enc[1:] + secret)

    def decrypt_e2e(self, data: bytes, key: str | bytes) -> bytes | None:
        """The plain bytes of a received E2E signal (``Signal.e2e``), or None for another key or a forgery."""
        boho = self._boho or Boho()
        return boho.decrypt_e2e(data, key)

    def _send_enc(self, packet: bytes) -> bool:
        """Encrypts signals, subscriptions, IAM and calls on a logged-in ws:// session."""
        if not self.is_encrypted:
            return self._send_raw(packet)
        enc = self._boho.encrypt_488(packet)
        return enc is not None and self._send_raw(enc)

    def _send_raw(self, packet: bytes) -> bool:
        if len(packet) > self.signal_size_limit:
            self._emit("over_size")
            return False
        if self.state not in _SENDABLE or self._queue is None:
            return False
        self._queue.put_nowait(packet)
        self.tx += 1
        return True

    # ---- connection

    def _connect(self, target: str) -> None:
        self._gen += 1
        self.connected_url = target
        self._tls = target.lower().startswith("wss://")
        self._quota_told = False
        self.max_message_bytes = None
        self._set_state(State.CONNECTING)
        self._hello_pending_since = time.monotonic()
        self._queue = asyncio.Queue()
        self._socket_task = asyncio.get_running_loop().create_task(self._run_socket(self._gen, target, self._queue))

    async def _run_socket(self, gen: int, target: str, queue: asyncio.Queue) -> None:
        transport: Transport | None = None
        writer: asyncio.Task | None = None
        error: BaseException | None = None
        try:
            transport = await self._connect_fn(target)
            if gen != self._gen:
                return
            self._last_rx = time.monotonic()
            self._ping_sent_at = 0.0
            self._set_state(State.OPEN)
            writer = asyncio.get_running_loop().create_task(self._write(transport, queue))
            while True:
                data = await transport.recv()
                if gen != self._gen:
                    return
                if isinstance(data, str):
                    continue
                self.rx += 1
                self._last_rx = time.monotonic()
                self._ping_sent_at = 0.0
                try:
                    self._on_data(data)
                except Exception as e:  # noqa: BLE001 - a bad packet must not stop the loop
                    self._emit("error", e)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            error = e
        finally:
            if writer is not None:
                writer.cancel()
            if transport is not None:
                try:
                    await asyncio.shield(transport.close())
                except Exception:  # noqa: BLE001
                    pass
            # A detached socket (close, stop, redirect, abandon) already moved the generation on.
            if gen == self._gen:
                self._dropped(error)

    async def _write(self, transport: Transport, queue: asyncio.Queue) -> None:
        try:
            while True:
                packet = await queue.get()
                await transport.send(packet)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - closing ends recv() in the socket task, which reports the drop
            await transport.close()

    def _reset_socket_state(self) -> None:
        self._socket_task = None
        self._queue = None
        self.cid = ""
        self._challenge = None
        self._ping_sent_at = 0.0
        self._hello_pending_since = 0.0
        if self._boho is not None:
            self._boho.deauthorize()
        self._fail_pending("Connection closed")

    def _detach(self) -> None:
        task = self._socket_task
        if task is None:
            return
        self._gen += 1
        self._reset_socket_state()
        task.cancel()

    def _dropped(self, error: BaseException | None) -> None:
        self._gen += 1
        self._reset_socket_state()
        self._schedule_reconnect()
        if self.state != State.STOP:
            self._set_state(State.CLOSED)
        if error is not None:
            self._emit("error", error)

    def _abandon(self, reason: str) -> None:
        """Drops a connection the server is not answering on."""
        if self._socket_task is None:
            return
        self._emit("error", ConnectionError(reason))
        self._detach()
        self._schedule_reconnect()
        if self.state != State.STOP:
            self._set_state(State.CLOSED)

    def _schedule_reconnect(self) -> None:
        self._next_connect_at = time.monotonic() + reconnect_delay(
            self._reconnect_attempts, random.random(), self.reconnect_base)
        self._reconnect_attempts += 1

    def _fail_pending(self, reason: str) -> None:
        pending, self._pending = self._pending, {}
        for fut in pending.values():
            if not fut.done():
                fut.set_exception(RpcError(reason))

    async def _keepalive_loop(self) -> None:
        while True:
            await asyncio.sleep(self.check_period)
            try:
                self._keepalive()
            except Exception as e:  # noqa: BLE001
                self._emit("error", e)

    def _keepalive(self) -> None:
        now = time.monotonic()
        if self._socket_task is None:
            if self.auto_reconnect and self.state != State.STOP and now >= self._next_connect_at:
                self._connect(self.url)
            return
        if self._hello_pending_since and now - self._hello_pending_since > self.hello_timeout:
            self._abandon(f"no reply from the server in {self.hello_timeout:g} s")
            return
        if self._ping_sent_at and now - self._ping_sent_at > self.pong_timeout:
            self._abandon(f"no reply from the server in {self.pong_timeout:g} s; reconnecting")
            return
        if not self._ping_sent_at and now - self._last_rx > self.ping_period:
            self.ping()

    def _redirect(self, new_url: str) -> None:
        self._set_state(State.REDIRECTING)
        self._detach()
        self._schedule_reconnect()  # url stays: when this connection ends, ask our own server again
        self._connect(new_url)

    def _set_state(self, state: State) -> None:
        if self.state == state:
            return
        self.state = state
        for entry in list(self._state_waiters):
            states, fut = entry
            if state in states and not fut.done():
                fut.set_result(state)
        self._emit("state", state)

    # ---- incoming packets

    def _on_data(self, received: bytes) -> None:
        if not received:
            return
        buffer = received
        if received[0] == IOMsg.ENC_488:
            buffer = self._boho.decrypt_488(received) if self._boho else None
            if buffer is None:
                return  # dropped, as in JS
        elif received[0] == IOMsg.ENC_E2E:  # session-encrypted SIGNAL_E2E header, then the E2E payload
            header = self._boho.decrypt_488(received) if self._boho else None
            if header is None:
                return
            buffer = header + bytes(received[ENC_488_META + len(header):])
        if not buffer:
            return
        t = buffer[0]
        if t == IOMsg.SERVER_TIME_NONCE:
            self._hello_pending_since = 0.0
            self._challenge = bytes(buffer)
            self._set_state(State.SERVER_READY)
            if self._boho is None:
                self._send_raw(bytes([IOMsg.CID_REQ]))
            else:
                packet = self._boho.auth_req(buffer)  # CID_REQ follows AUTH_RES
                if packet is None:
                    self._set_state(State.AUTH_FAIL)
                    self._emit("error", ValueError("malformed SERVER_TIME_NONCE"))
                elif self._send_raw(packet):
                    self._set_state(State.AUTH_REQ)
        elif t == IOMsg.AUTH_RES:
            if self._boho is not None and self._boho.verify_auth_res(buffer):
                self._set_state(State.AUTH_RES)
                self._send_raw(bytes([IOMsg.CID_REQ]))
            else:
                self._set_state(State.AUTH_FAIL)
        elif t == IOMsg.AUTH_FAIL:
            if self._boho is not None:
                self._boho.deauthorize()
            self._set_state(State.AUTH_FAIL)
        elif t == IOMsg.CID_RES:
            self.cid = buffer[1:].decode()
            self._reconnect_attempts = 0
            self._set_state(State.READY)  # before re-subscribing, as the JS client does
            for batch in subscription_batches(self._channels):
                self._send_enc(tagged(IOMsg.SUBSCRIBE, encode_tag(batch)))
            self._emit("ready", self.cid)
        elif t == IOMsg.SIGNAL or t == IOMsg.SIGNAL_E2E:
            signal = decode_signal(buffer)
            if signal is not None:
                signal.e2e = t == IOMsg.SIGNAL_E2E
                self._dispatch(signal)
        elif t == IOMsg.PING:
            self._send_raw(bytes([IOMsg.PONG]))
        elif t == IOMsg.PONG:
            self._emit("pong")
        elif t == IOMsg.QUOTA_LEVEL:
            if len(buffer) > 1:
                self.quota_level = buffer[1]
        elif t == IOMsg.QUOTA:
            if len(buffer) >= 5:
                self.max_message_bytes = max_message_bytes(buffer)
                self._quota_told = True
        elif t == IOMsg.IAM_RES:
            self._emit("iam", _json_or_text(buffer[1:]))
        elif t == IOMsg.SERVER_SIGNAL:
            self._emit("server_signal", _json_or_text(buffer[1:]))
        elif t == IOMsg.OVER_SIZE:
            self._emit("over_size")
        elif t == IOMsg.RESPONSE_MBP:
            self._on_response(buffer)
        elif t == IOMsg.AUTH_CLEAR:
            # Duplicate login (keepNew), or a members-only server refusing an anonymous connection.
            self.clear_auth()
            self._set_state(State.AUTH_CLEAR)
            self.stop()
        elif t == IOMsg.SERVER_REDIRECT:
            if len(buffer) == 7:  # IPv4 and port: a TCP (cong://) server
                self._redirect("cong://%d.%d.%d.%d:%d" % (buffer[1], buffer[2], buffer[3], buffer[4],
                                                          (buffer[5] << 8) | buffer[6]))
            else:
                self._redirect(buffer[1:].decode())

    def _on_response(self, buffer: bytes) -> None:
        res = mbp.unpack(buffer)
        if res is None or not isinstance(res.get("mid"), int):
            return
        mid = res["mid"]
        status = res.get("status", 255)
        fut = self._pending.pop(mid, None)
        if fut is None or fut.done():
            return
        response = RpcResponse(mid, status if isinstance(status, int) else 255, res.get("body"), dict(res.fields))
        if response.ok:
            fut.set_result(response)
        else:
            fut.set_exception(RpcError(f"rpc failed: status {response.status}", response))

    def _dispatch(self, signal: Signal) -> None:
        """Same order as JS: direct signals go to ``@`` and their tag, others to the tag; then the event."""
        if signal.is_direct:
            self._invoke("@", signal)
            if signal.tag != "@":
                self._invoke(signal.tag, signal)
            self._emit("direct", signal)
        else:
            self._invoke(signal.tag, signal)
            self._emit("message", signal)

    def _invoke(self, tag: str, signal: Signal) -> None:
        for handler in list(self._handlers.get(tag, ())):
            self._call(handler, (signal,))

    def _emit(self, name: str, *args: Any) -> None:
        handlers = self._listeners[name]
        if name == "error" and not handlers:
            log.warning("iosignal error: %r", args[0] if args else None)
            return
        for handler in list(handlers):
            self._call(handler, args, report=name != "error")

    def _call(self, handler: Callable, args: tuple, report: bool = True) -> None:
        try:
            result = handler(*args)
            if inspect.isawaitable(result):
                task = asyncio.ensure_future(result)
                self._tasks.add(task)
                task.add_done_callback(self._task_done)
        except Exception as e:  # noqa: BLE001
            if report:
                self._emit("error", e)
            else:
                log.exception("iosignal error handler raised")

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            self._emit("error", task.exception())
