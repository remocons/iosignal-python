"""Controller side of the sealed board REPL (``repl.1``, iosignal.replproto): a session, mpremote's raw REPL on
it, and fast file transfer handled by the board's link (not through the REPL). CPython (asyncio).

    io = IOSignal(url); io.open(); await io.ready()
    s = ReplSession(io, "mpdev5", key)
    await s.open()
    await s.run("1+1\\r", r"2")          # REPL
    await s.files.put("/main.py", data)  # files
"""

import asyncio
import hashlib
import json
import os
import re
import struct
import time

from .client import IOSignal
from .replproto import (
    CLOSE, CLOSED, DATA, FS, FS_RES, IN, OPEN, OPENED, OUT, Sealer, from_hex, session_key, session_key2, to_hex,
)

__all__ = ["ReplSession", "RawRepl", "Files", "FsError", "load_key"]


def load_key(path=None) -> bytes:
    """The REPL key from a hex file (``REPL_KEY_FILE`` when no path is given)."""
    path = path or os.environ.get("REPL_KEY_FILE", "")
    with open(os.path.expanduser(path)) as f:
        return from_hex(f.read().strip())


class FsError(OSError):
    pass


# Typed, pasted and piped input: 1 kB frames, a short burst at once, then at most IN_PER_SEC a second, so a long
# paste stays below the server's message quota (anonymous: 50/s on io.iosignal.net) instead of losing frames.
# The same rule as the web IDE (web/replclient.js).
IN_CHUNK = 1024
IN_PER_SEC = 30
IN_BURST = 10


class ReplSession:
    """A sealed REPL session with a board running iosignal.replpoll."""

    def __init__(self, io: IOSignal, board: str, key: bytes, forward_secrecy: bool = True):
        self.io, self.board, self.key = io, board, key
        self.base = Sealer(key)
        self.sealer = None
        self.cnonce = None
        self.forward_secrecy = forward_secrecy  # stage 2: one-time X25519 keys (falls back on an older board)
        self.eph = None                          # (secret, public) of the open in flight
        self.stage = None                        # 1 or 2 once open
        self.tx_seq = self.rx_seq = 0
        self.buf = ""
        self.raw = b""
        self.info = None
        self.frames = 0
        self.dropped = 0
        self.sent = []  # sealed packets sent (kept for replay checks)
        self.on_output = None  # function(bytes): REPL output as it arrives
        self.on_closed = None  # function(reason): the board closed the session (close, taken)
        self.changed = asyncio.Event()
        self.files = Files(self)
        self._in = []           # input frames waiting for their turn
        self._in_tokens = float(IN_BURST)
        self._in_at = time.monotonic()
        self._in_timer = None
        io.on("@repl", self._rx)

    def _rx(self, s):
        packet = s.data
        if self.sealer is not None:
            frame = self.sealer.open(packet)
            if frame is not None:
                kind, seq, data = frame
                if seq <= self.rx_seq:
                    self.dropped += 1
                    return
                self.rx_seq = seq
                if kind == OUT:
                    self.frames += 1
                    self.raw += data
                    self.buf += data.decode("utf-8", "replace")
                    if self.on_output:
                        self.on_output(data)
                elif kind == CLOSED:
                    self.buf += "[closed %s]" % data.decode()
                    if self.on_closed:
                        self.on_closed(data.decode())
                elif kind == FS_RES:
                    self.files._reply(json.loads(data))
                elif kind == DATA:
                    self.files._data(data)
                self.changed.set()
                return
        frame = self.base.open(packet)
        if frame is None or frame[0] != OPENED or self.cnonce is None:
            self.dropped += 1
            return
        body = json.loads(frame[2])
        if body.get("cnonce") != to_hex(self.cnonce):
            self.dropped += 1
            return
        bnonce = from_hex(body["bnonce"])
        if body.get("b") and self.eph:
            from .x25519 import shared
            b_pub = from_hex(body["b"])
            try:
                skey = session_key2(self.key, self.cnonce, bnonce, self.eph[1], b_pub, shared(self.eph[0], b_pub))
            except ValueError:
                self.dropped += 1
                return
            self.stage = 2
        else:
            skey = session_key(self.key, self.cnonce, bnonce)
            self.stage = 1
        self.eph = None
        self.sealer = Sealer(skey)
        self.rx_seq = frame[1]
        self.tx_seq = 0
        self.info = body["info"]
        self.changed.set()

    def send_frame(self, kind, data=b"", sealer=None):
        self.tx_seq += 1
        packet = (sealer or self.sealer).seal(kind, self.tx_seq, data)
        self.sent.append(packet)
        self.io.signal(self.board + "@repl", packet)
        return packet

    async def open(self, timeout=5.0):
        """Opens the session; returns the board info, or None when the board does not answer."""
        self.cnonce = os.urandom(16)
        self.sealer, self.info, self.tx_seq = None, None, 0
        req = {"v": 1, "reply": self.io.cid, "cnonce": to_hex(self.cnonce), "ts": int(time.time())}
        if self.forward_secrecy:
            from .x25519 import public
            secret = os.urandom(32)
            self.eph = (secret, public(secret))
            req["v"], req["a"] = 2, to_hex(self.eph[1])
        self.send_frame(OPEN, json.dumps(req).encode(), self.base)
        end = time.monotonic() + timeout
        while self.info is None and time.monotonic() < end:
            await asyncio.sleep(0.02)
        return self.info

    def close(self):
        self._in.clear()  # input not sent yet is dropped
        if self._in_timer:
            self._in_timer.cancel()
            self._in_timer = None
        self.send_frame(CLOSE)

    def send(self, text):
        """Input for the board's REPL, paced (IN_BURST, then IN_PER_SEC frames of IN_CHUNK bytes). A lone Ctrl-C
        while a paste is still going out drops the rest of it and goes first."""
        data = text.encode() if isinstance(text, str) else bytes(text)
        if data == b"\x03" and self._in:
            self._in.clear()
            self.send_frame(IN, data)
            return
        self._in.extend(data[i:i + IN_CHUNK] for i in range(0, len(data), IN_CHUNK))
        self._drain_in()

    def _drain_in(self):
        self._in_timer = None
        now = time.monotonic()
        self._in_tokens = min(IN_BURST, self._in_tokens + (now - self._in_at) * IN_PER_SEC)
        self._in_at = now
        while self._in and self._in_tokens >= 1:
            self._in_tokens -= 1
            self.send_frame(IN, self._in.pop(0))
        if self._in:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:  # no event loop (not how the CLI runs): send the rest at once
                while self._in:
                    self.send_frame(IN, self._in.pop(0))
                return
            self._in_timer = loop.call_later(1 / IN_PER_SEC, self._drain_in)

    async def drain_input(self):
        """Waits until all input given to send() has gone out."""
        while self._in:
            await asyncio.sleep(1 / IN_PER_SEC)

    async def wait(self, cond, timeout):
        end = time.monotonic() + timeout
        while not cond():
            left = end - time.monotonic()
            if left <= 0:
                raise TimeoutError("timed out")
            self.changed.clear()
            try:
                await asyncio.wait_for(self.changed.wait(), left)
            except asyncio.TimeoutError:
                pass

    async def expect(self, pattern, timeout=10.0, start=0):
        """Waits until the output after ``start`` matches; returns (match, end position)."""
        try:
            await self.wait(lambda: re.search(pattern, self.buf[start:], re.S), timeout)
        except TimeoutError:
            raise AssertionError("no %r in output: %r" % (pattern, self.buf[start:][-300:])) from None
        m = re.search(pattern, self.buf[start:], re.S)
        return m, start + m.end()

    async def run(self, line, pattern, timeout=10.0):
        start = len(self.buf)
        self.send(line)
        return await self.expect(pattern, timeout, start)


class Files:
    """File operations done by the board's link itself: binary chunks, a window in flight, SHA-256 checked."""

    def __init__(self, s: ReplSession):
        self.s = s
        self.replies = []
        self.acked = 0
        self.incoming = {}
        self.end = None

    def _reply(self, body):
        if body.get("op") == "ack":
            self.acked = max(self.acked, body["at"])
        elif body.get("op") == "end":
            self.end = body
        else:
            self.replies.append(body)

    def _data(self, data):
        self.incoming[int.from_bytes(data[:4], "little")] = data[4:]

    async def request(self, op, timeout=10.0, **args):
        # Each reply carries its op: a late reply to something else (an abort, a timed-out request) is not taken.
        self.replies.clear()
        self.s.send_frame(FS, json.dumps(dict(args, op=op)).encode())

        def mine():
            return next((i for i, r in enumerate(self.replies) if r.get("op") == op), None)
        try:
            await self.s.wait(lambda: mine() is not None, timeout)
        except TimeoutError:
            raise FsError("%s: no reply" % op) from None
        body = self.replies.pop(mine())
        if not body.get("ok"):
            raise FsError("%s %s: %s" % (op, args.get("path", ""), body.get("error")))
        return body

    async def put(self, path: str, data: bytes, chunk=1400, window=8192, timeout=30.0, max_frames_per_sec=40,
                  stall=15.0):
        """Writes ``data`` to ``path`` on the board; it replaces the file only when size and SHA-256 match.

        ``max_frames_per_sec`` paces the data frames below the server's message quota (anonymous: 50/s on
        io.iosignal.net; a frame the server drops makes the board refuse the rest), the same as the web IDE.
        The upload stops at once on a quota notice or a refusal from the board, and after ``stall`` seconds
        without an ack; the board then gets an abort and drops its .part file."""
        quota = {}

        def on_server(body):
            if isinstance(body, dict) and body.get("event") == "quota_exceeded":
                quota.update(body.get("data") or {"kind": "quota"})
                self.s.changed.set()

        def failure():
            if quota:
                return "the server's quota stopped the upload (%s %s, %s)" % (
                    quota.get("kind"), quota.get("limit"), quota.get("quota"))
            refused = next((r for r in self.replies if r.get("op") == "put" and not r.get("ok", True)), None)
            if refused is None:
                return None
            if "offset" in str(refused.get("error")):
                return "a frame went missing and the board stopped the upload (server quota or link: %s)" % refused.get("error")
            return "the board stopped the upload: %s" % refused.get("error")

        self.s.io.on_event("server_signal", on_server)
        try:
            await self.request("put", path=path, size=len(data))
            self.acked = 0
            gap = 1.0 / max_frames_per_sec if max_frames_per_sec else 0.0
            at, nxt = 0, 0.0
            try:
                while at < len(data):
                    last = self.acked
                    while True:  # room in the window; give up when acks stop for ``stall`` seconds
                        try:
                            await self.s.wait(lambda: failure() or at - self.acked < window, stall)
                            break
                        except TimeoutError:
                            if self.acked != last:
                                last = self.acked
                                continue
                            raise FsError("the board did not answer for %g s; upload stopped at %d of %d bytes"
                                          % (stall, self.acked, len(data))) from None
                    why = failure()
                    if why:
                        raise FsError(why)
                    now = time.monotonic()
                    if nxt > now:
                        await asyncio.sleep(nxt - now)
                    nxt = max(time.monotonic(), nxt) + gap
                    part = data[at:at + chunk]
                    self.s.send_frame(DATA, struct.pack("<I", at) + part)
                    at += len(part)
                return await self.request("close", timeout, sha256=hashlib.sha256(data).hexdigest())
            except BaseException:
                try:
                    self.s.send_frame(FS, json.dumps({"op": "abort"}).encode())  # the board drops its .part file
                except Exception:  # noqa: BLE001 - best effort
                    pass
                raise
        finally:
            self.s.io.off_event("server_signal", on_server)

    async def get(self, path: str, timeout=30.0) -> bytes:
        self.incoming, self.end = {}, None
        res = await self.request("get", path=path)
        await self.s.wait(lambda: self.end is not None, timeout)
        data = b"".join(self.incoming[k] for k in sorted(self.incoming))
        if len(data) != res["size"] or hashlib.sha256(data).hexdigest() != self.end["sha256"]:
            raise FsError("get %s: size or SHA-256 differs" % path)
        return data

    async def ls(self, path="/"):
        return (await self.request("ls", path=path))["items"]

    async def stat(self, path):
        return await self.request("stat", path=path)

    async def rm(self, path):
        await self.request("rm", path=path)

    async def mkdir(self, path):
        await self.request("mkdir", path=path)

    async def rmdir(self, path):
        await self.request("rmdir", path=path)

    async def rename(self, path, to):
        await self.request("rename", path=path, to=to)


class RawRepl:
    """mpremote's raw REPL protocol on a session's byte stream (exec of code; files go through Files)."""

    def __init__(self, s: ReplSession):
        self.s = s
        self.pos = 0
        self.paste_window = None

    async def read_until(self, token: bytes, timeout=10.0) -> bytes:
        try:
            await self.s.wait(lambda: self.s.raw.find(token, self.pos) >= 0, timeout)
        except TimeoutError:
            raise AssertionError("timed out; last bytes %r" % self.s.raw[self.pos:][-120:]) from None
        at = self.s.raw.find(token, self.pos) + len(token)
        data, self.pos = self.s.raw[self.pos:at], at
        return data

    async def read_n(self, n: int, timeout=10.0) -> bytes:
        await self.s.wait(lambda: len(self.s.raw) - self.pos >= n, timeout)
        data, self.pos = self.s.raw[self.pos:self.pos + n], self.pos + n
        return data

    async def enter(self):
        self.pos = len(self.s.raw)
        self.s.send(b"\r\x03\x03\x01")  # stop anything running, then raw REPL
        await self.read_until(b"raw REPL; CTRL-B to exit\r\n>", 10)

    async def exit(self):
        self.pos = len(self.s.raw)
        self.s.send(b"\x02")
        await self.read_until(b">>> ", 5)

    async def _follow(self, timeout):
        out = await self.read_until(b"\x04", timeout)
        err = await self.read_until(b"\x04", timeout)
        await self.read_until(b">", timeout)
        return out[:-1], err[:-1]

    async def exec_raw(self, code: bytes, timeout=20.0):
        """Plain raw mode: the whole code at once, then Ctrl-D (no flow control)."""
        self.pos = len(self.s.raw)
        self.s.send(code + b"\x04")
        await self.read_until(b"OK", timeout)
        return await self._follow(timeout)

    async def exec_paste(self, code: bytes, timeout=20.0):
        """raw-paste mode with the device's flow-control window (what mpremote uses)."""
        self.pos = len(self.s.raw)
        self.s.send(b"\x05A\x01")
        answer = await self.read_n(2, timeout)
        if answer != b"R\x01":
            raise AssertionError("raw-paste not supported: %r" % answer)
        window = struct.unpack("<H", await self.read_n(2, timeout))[0]
        self.paste_window = window
        remain, i = window, 0
        while i < len(code):
            while remain == 0:
                b = await self.read_n(1, timeout)
                if b == b"\x01":
                    remain += window
                else:
                    raise AssertionError("unexpected byte in raw-paste: %r" % b)
            part = code[i:i + remain]
            self.s.send(part)
            remain -= len(part)
            i += len(part)
        self.s.send(b"\x04")
        while True:  # window increments may still be on their way; the device ends the data with \x04
            b = await self.read_n(1, timeout)
            if b == b"\x04":
                break
            if b != b"\x01":
                raise AssertionError("unexpected byte after raw-paste data: %r" % b)
        return await self._follow(timeout)
