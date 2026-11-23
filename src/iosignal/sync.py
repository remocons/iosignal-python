"""Blocking IOSignal client for scripts: the asyncio client runs on a background thread.

    from iosignal.sync import IOSignal

    with IOSignal("ws://127.0.0.1:7777") as io:
        io.ready()
        inbox = io.listen("room#t")          # no handler: a queue.Queue of Signals
        io.signal("room#t", "hello")
        print(inbox.get(timeout=3).text)

Handlers given to :meth:`listen`, :meth:`on` and :meth:`on_event` run on the background thread; keep them
short, or use a queue. Every method may be called from any thread, handlers included.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import queue
import threading
from typing import Any, Callable, TypeVar

from . import client as _client
from . import devices as _devices
from .protocol import Signal, State

__all__ = ["IOSignal"]

T = TypeVar("T")


class IOSignal:
    """Blocking counterpart of :class:`iosignal.client.IOSignal` (same arguments and methods)."""

    def __init__(self, url: str | None = None, **kwargs: Any):
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="iosignal", daemon=True)
        self._thread.start()
        self._closed = False
        self.aio: _client.IOSignal = self.run(lambda: _client.IOSignal(url, **kwargs))

    # ---- running on the loop

    def _on_loop(self) -> bool:
        return threading.current_thread() is self._thread

    def run(self, fn: Callable[[], T]) -> T:
        """Runs ``fn()`` on the client's thread and returns its result (direct when already there)."""
        if self._on_loop():
            return fn()
        if self._closed:
            raise RuntimeError("IOSignal is closed")
        fut: concurrent.futures.Future = concurrent.futures.Future()

        def call() -> None:
            try:
                fut.set_result(fn())
            except BaseException as e:  # noqa: BLE001
                fut.set_exception(e)

        self._loop.call_soon_threadsafe(call)
        return fut.result()

    def wait(self, coro: Any, timeout: float | None = None) -> Any:
        """Runs a coroutine on the client's thread and waits for it. Not from a handler (it would deadlock)."""
        if self._on_loop():
            coro.close()
            raise RuntimeError("cannot wait from a handler; use the asyncio client there")
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return fut.result(timeout)
        except concurrent.futures.TimeoutError:
            if fut.done():  # the coroutine's own TimeoutError (the same class from Python 3.11)
                raise
            # Python 3.10: concurrent.futures.TimeoutError is not the builtin TimeoutError.
            fut.cancel()
            raise TimeoutError(f"no result in {timeout:g} s") from None

    # ---- state

    @property
    def cid(self) -> str:
        return self.aio.cid

    @property
    def state(self) -> State:
        return self.aio.state

    @property
    def is_authorized(self) -> bool:
        return self.aio.is_authorized

    @property
    def is_encrypted(self) -> bool:
        return self.aio.is_encrypted

    @property
    def subscriptions(self) -> list[str]:
        return self.run(lambda: self.aio.subscriptions)

    def __getattr__(self, name: str) -> Any:
        # Plain attributes of the asyncio client (url, auth_id, quota_level, check_period, ...).
        if name.startswith("_") or name == "aio":
            raise AttributeError(name)
        value = getattr(self.aio, name)
        if callable(value):
            raise AttributeError(name)
        return value

    def set(self, name: str, value: Any) -> None:
        """Sets an attribute of the asyncio client, such as ``auto_reconnect`` or ``check_period``."""
        self.run(lambda: setattr(self.aio, name, value))

    # ---- lifecycle

    def open(self, url: str | None = None) -> IOSignal:
        self.run(lambda: self.aio.open(url))
        return self

    def ready(self, timeout: float | None = 10.0) -> str:
        """Waits for READY and returns the CID (AuthError, TimeoutError)."""
        return self.wait(self.aio.ready(timeout=timeout))

    def wait_for(self, *states: State, timeout: float | None = None) -> State:
        return self.wait(self.aio.wait_for(*states, timeout=timeout))

    def close(self) -> None:
        """Closes the socket; with auto_reconnect the client comes back later. :meth:`shutdown` ends it."""
        self.run(self.aio.close)

    def stop(self) -> None:
        self.run(self.aio.stop)

    def shutdown(self) -> None:
        """Stops the client and its thread. The instance is unusable afterward."""
        if self._closed:
            return
        if self._on_loop():
            raise RuntimeError("shutdown() from a handler would deadlock")
        try:
            asyncio.run_coroutine_threadsafe(self.aio.aclose(), self._loop).result(5)
        except concurrent.futures.TimeoutError:
            _client.log.warning("iosignal: tasks did not end in 5 s; stopping the loop anyway")
        finally:
            self._closed = True
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(5)
            if not self._thread.is_alive():  # closing a loop that still runs raises
                self._loop.close()

    def __enter__(self) -> IOSignal:
        if self.state in (State.CLOSED, State.STOP) and self.aio.url:
            self.open()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.shutdown()

    # ---- login

    def auth(self, id_: str, key: str | None = None) -> IOSignal:
        self.run(lambda: self.aio.auth(id_, key))
        return self

    def login(self, id_: str, key: str | None = None) -> IOSignal:
        self.run(lambda: self.aio.login(id_, key))
        return self

    def clear_auth(self) -> None:
        self.run(self.aio.clear_auth)

    # ---- receiving

    def on_event(self, name: str, handler: Callable | None = None):
        """Registers an event handler; without one, returns a queue.Queue of the event's argument."""
        if handler is None:
            q: queue.Queue = queue.Queue()
            self.run(lambda: self.aio.on_event(name, lambda *a: q.put(a[0] if len(a) == 1 else a)))
            return q
        return self.run(lambda: self.aio.on_event(name, handler))

    def off_event(self, name: str, handler: Callable) -> None:
        self.run(lambda: self.aio.off_event(name, handler))

    def on(self, tag: str, handler: Callable[[Signal], Any] | None = None):
        """Tag handler without subscribing; without one, returns a queue.Queue of Signals."""
        q, handler = self._queue_or(handler)
        self.run(lambda: self.aio.on(tag, handler))
        return q if q is not None else handler

    def off(self, tag: str, handler: Callable | None = None) -> None:
        self.run(lambda: self.aio.off(tag, handler))

    def listen(self, tag: str, handler: Callable[[Signal], Any] | None = None):
        """:meth:`on` plus :meth:`subscribe`; without a handler, returns a queue.Queue of Signals."""
        q, handler = self._queue_or(handler)
        self.run(lambda: self.aio.listen(tag, handler))
        return q if q is not None else handler

    @staticmethod
    def _queue_or(handler: Callable | None) -> tuple[queue.Queue | None, Callable]:
        if handler is not None:
            return None, handler
        q: queue.Queue = queue.Queue()
        return q, q.put

    def subscribe(self, tags: str) -> bool:
        return self.run(lambda: self.aio.subscribe(tags))

    def unsubscribe(self, tags: str) -> bool:
        return self.run(lambda: self.aio.unsubscribe(tags))

    def unsubscribe_all(self) -> bool:
        return self.run(self.aio.unsubscribe_all)

    # ---- sending

    def signal(self, tag: str, *values: Any) -> bool:
        return self.run(lambda: self.aio.signal(tag, *values))

    def signal_e2e(self, tag: str, data: bytes | str, key: str | bytes) -> bool:
        return self.run(lambda: self.aio.signal_e2e(tag, data, key))

    def decrypt_e2e(self, data: bytes, key: str | bytes) -> bytes | None:
        return self.aio.decrypt_e2e(data, key)

    def iam(self, nick: str | None = None) -> bool:
        return self.run(lambda: self.aio.iam(nick))

    def ping(self) -> bool:
        return self.run(self.aio.ping)

    def ping_server(self, timeout: float = 3.0) -> float:
        """Blocking round trip to the server in milliseconds; raises PingError like the asyncio client."""
        return self.wait(self.aio.ping_server(timeout))

    def call(self, target: str, topic: str, *args: Any, timeout: float | None = None) -> _client.RpcResponse:
        """Blocking remote call; raises RpcError like the asyncio client."""
        return self.wait(self.aio.call(target, topic, *args, timeout=timeout))

    # ---- devices

    def device(self, name: str, group: str = "", **kwargs: Any) -> _devices.Device:
        """A :class:`iosignal.devices.Device` on this client. Change it with ``io.run(lambda: lamp.set(on=1))``;
        its ``on_command`` runs on the client's thread."""
        return self.run(lambda: _devices.Device(self.aio, name, group, **kwargs))

    def discover(self, wait: float = 1.5, panels: bool = True, views: bool = False) -> list[_devices.DeviceInfo]:
        return self.wait(_devices.discover(self.aio, wait, panels, views))

    def command(self, device: _devices.DeviceInfo | str, cmd: str) -> bool:
        return self.run(lambda: _devices.command(self.aio, device, cmd))
