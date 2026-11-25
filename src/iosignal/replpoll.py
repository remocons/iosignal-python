"""REPL over IOSignal for MicroPython boards, single-threaded (spike S3, second design).

A ``machine.Timer`` callback (run by MicroPython's scheduler on the main thread, between bytecodes, also
inside busy loops and ``time.sleep``) polls a non-blocking TCP connection (``cong://``), handles IOSignal
packets (Boho login, ENC on Ascon), flushes the REPL output and keeps the connection alive. The REPL is mirrored
with ``os.dupterm``. Ctrl-C from the controller is fed through dupterm as the last action of a callback, so
MicroPython raises KeyboardInterrupt in the running code (an exception inside a scheduled callback would be
caught and printed instead).

Why not a thread: on ESP32 (MicroPython 1.29) a second Python thread is starved while the main thread runs a
busy loop (the GIL hand-off is unfair), so Ctrl-C arrived only after the loop ended.

Frames are sealed with the REPL key and a per-session key, with sequence numbers (``replproto``):
the broker relays only sealed bytes, and only a controller holding the REPL key can open a session.

    controller -> <board>@repl   1 open    3 in <bytes>    5 close
    board -> <controller>@repl   2 opened  4 out <bytes>   6 closed <reason>

The link takes the highest hardware timer the chip has (ESP32, S3: Timer(3); C3, C6: Timer(1)), so code
copied from tutorials, which start at Timer(0), does not take it over; ``timer=`` (``"timer"`` in
/iosignal.json) picks another. Two users of one timer: the last ``init`` wins and the other stops silently.

MicroPython only (machine.Timer, os.dupterm).
"""

import hashlib
import io
import json
import os
import socket
import sys
import time

import machine

from . import cong
from .boho import Boho
from .protocol import IOMsg, decode_signal, encode_payload, encode_tag, signal_packet
from .replproto import (
    CLOSE, CLOSED, DATA, FS, FS_RES, IN, OPEN, OPEN_MAX_AGE_S, OPENED, OUT, Sealer, from_hex, session_key, session_key2, to_hex,
)
_POLL, _POLL_RD, _POLL_WR = 3, 0x0001, 0x0004
FRAME = 900
OUT_CAP = 4096
TX_CAP = 16384
PING_MS, PONG_MS, HELLO_MS = 30000, 12000, 10000
ACTIVE_PING_MS = 10000  # while sending: ping this long after the last PONG, though data keeps coming in
STALL_MS = 10000        # data waiting to go out and not one byte sent for this long: the uplink is stuck
RECV = 4096          # bytes read per tick
FS_ACK_EVERY = 4096  # put: ack after this many bytes written
GET_CHUNK = 1536     # get: bytes per DATA frame
GET_PER_TICK = 2     # get: frames per tick (keeps the REPL responsive)
PONG_PACKET = bytes([IOMsg.PONG])
LINKLOG = "/linklog.txt"  # drops and reconnects (off with "linklog": false in /iosignal.json)
LINKLOG_MAX = 4096
BUSY_MS = 8          # a tick that works longer makes the link rest as long, so it takes at most about half the CPU


class Term(io.IOBase):
    """The REPL's extra terminal: ``write`` gets REPL output, ``readinto`` gives the REPL the controller's input."""

    def __init__(self):
        self.inbuf = bytearray()
        self.out = bytearray()
        self.active = False
        self.dropped = 0

    def readinto(self, buf):
        n = min(len(buf), len(self.inbuf))
        if not n:
            return None
        buf[:n] = self.inbuf[:n]
        self.inbuf = self.inbuf[n:]
        return n

    def write(self, buf):
        n = len(buf)
        if self.active:
            room = OUT_CAP - len(self.out)
            if n > room:
                self.dropped += n - room
                buf = buf[:room]
            self.out += buf
        return n

    def ioctl(self, req, arg):
        if req == _POLL:
            return (_POLL_RD if arg & _POLL_RD and self.inbuf else 0) | (arg & _POLL_WR)
        return 0

    def feed(self, data):
        # The REPL reads this stream directly whenever its 260-byte stdin ring buffer is empty
        # (mp_hal_stdin_rx_chr), so input waits here at any size. dupterm_notify would copy it into that
        # ring buffer, which drops what does not fit; only Ctrl-C needs it (see interrupt).
        self.inbuf += data

    def interrupt(self):
        """Ctrl-C into running code: dupterm_notify reads up to the \\x03 and raises KeyboardInterrupt."""
        self.inbuf += b"\x03"
        os.dupterm_notify(None)


class ReplLink:
    def __init__(self, url, key, auth=None, period_ms=20, wifi=None, name=None, announce=True, txpower=None,
                 wifi_mode=None, linklog=True, timer=None):
        if url.startswith("cong://"):
            url = url[7:]
        host, _, port = url.rpartition(":")
        self.target = (host, int(port))
        self.auth = auth
        self.period_ms = period_ms
        self.term = Term()
        self.sock = None
        self.cid = ""
        self.ready = False
        self.owner = None
        self.key = bytes(key) if key else None
        self.base = Sealer(self.key) if key else None  # open/opened; None: no remote REPL (a device only)
        self.session = None           # Sealer with the session key
        self.tx_seq = self.rx_seq = 0
        self.seen_opens = {}          # cnonce -> unix seconds, for OPEN_MAX_AGE_S
        self.fs = None                # the file being sent or received
        self.wifi = wifi              # (ssid, password): the link brings Wi-Fi up itself; None: someone else does
        self.txpower = txpower        # Wi-Fi send power in dBm, e.g. 8.5; None: the chip's default (the most)
        self.wifi_mode = wifi_mode    # "b" (802.11b only: slow but reaches farther), "bg", "bgn"; None: as is
        self.linklog = LINKLOG if linklog else None  # drops and reconnects, a line each, for weak spots
        self.online_at = None         # ticks_ms when the link last became ready
        self.down_at = None           # ticks_ms of the last logged drop (None: none since the last online)
        self.logged_start = False
        self.name = name              # $name ("name:category"); None: no announcement of a name
        self.announce = announce      # answer #search and publish $name/$ui/$state (device rules)
        self.device = None            # an mdevice.Device: the board is that device (set_device)
        self.listeners = {}           # tag -> [fn(tag, value)]: signals the board's own code follows (subscribe)
        self.wlan = None
        self.wifi_at = 0
        self.wifi_tuned = False       # _wifi_tune ran on this Wi-Fi connection
        self.busy_us = 0              # time spent in _tick since busy_since (reported by the "info" request)
        self.busy_since = time.ticks_ms()
        self.clock_ok = False
        self.clock_try = 0
        self.addr = None
        self.pending = None           # (socket, started) while a non-blocking connect is in progress
        self.rest_until = None        # ticks_ms until which the link leaves the CPU to the REPL
        self.eph = None               # (secret, public) X25519 ready for the next stage 2 open (made when idle)
        self.boho = None
        self.parser = None
        self.tx = b""
        self.intr = False
        self.after = b""
        self.next_try = 0
        self.backoff = 1000
        self.opened_at = self.last_rx = self.ping_at = 0
        self.ping_queued = False      # a PING is still in self.tx: its wait starts when it has gone out
        self.pong_at = 0              # ticks_ms of the last PONG (or of connecting)
        self.sent_since_pong = False  # something besides PING/PONG went out since then
        self.tx_moved_at = 0          # ticks_ms when the send buffer last got data out (or got its first byte)
        self.errors = []
        self.stats = {"rx": 0, "tx": 0, "ticks": 0, "connects": 0, "refused": 0, "replayed": 0}
        self.timer = None
        self.timer_id = timer         # hardware timer number; None: the highest one (last_timer)

    # ---- lifecycle

    def start(self):
        if self.base is not None:
            os.dupterm(self.term)
        if self.timer_id is None:
            self.timer_id = last_timer()
        self.timer = machine.Timer(self.timer_id)
        self.timer.init(period=self.period_ms, mode=machine.Timer.PERIODIC, callback=self._tick)
        return self

    def stop(self):
        if self.timer:
            self.timer.deinit()
            self.timer = None
        os.dupterm(None)
        self._drop("stopped")

    def _error(self, text):
        self.errors = (self.errors + [text])[-5:]

    # ---- the timer callback (main thread, between bytecodes)

    def _tick(self, _t):
        now = time.ticks_ms()
        if self.rest_until is not None:
            if time.ticks_diff(self.rest_until, now) > 0:
                return
            self.rest_until = None
        started = time.ticks_us()
        try:
            self.stats["ticks"] += 1
            if self.after and not self.intr:
                after, self.after = self.after, b""
                self._input(after)
            self._poll()
            if self.device is not None and self.device.pending:
                self.device.flush()
            if self.eph is None and self.ready and self.owner is None and self.base is not None and not self.tx:
                self.eph = self._new_eph()  # ~0.5 s on an ESP32-S3: done while nobody waits for it; False: no X25519
        except Exception as e:  # noqa: BLE001 - must never escape into the user's code
            self._error("tick %s: %s" % (type(e).__name__, e))
            self._drop("error")
        spent_us = time.ticks_diff(time.ticks_us(), started)
        self.busy_us += spent_us
        spent = spent_us // 1000
        if spent > BUSY_MS:  # e.g. a file transfer: leave the main thread (the REPL, user code) as much time
            self.rest_until = time.ticks_add(now, 2 * spent)
            self.stats["rests"] = self.stats.get("rests", 0) + 1
        if self.intr:  # last: the KeyboardInterrupt is raised in the code that runs after this callback
            self.intr = False
            self.term.interrupt()

    def _poll(self):
        now = time.ticks_ms()
        if self.sock is None:
            self._bring_up(now)
            return
        if self.wlan is not None and not self.wlan.isconnected():
            self._drop("wifi lost")
            return
        data = None
        try:
            data = self.sock.recv(RECV)
        except OSError as e:
            if e.args[0] not in (11, 115, 119):  # EAGAIN, EINPROGRESS, EALREADY
                self._error("recv %s" % e)
                self._drop("recv")
                return
        if data is not None:
            if not data:
                self._drop("closed by server")
                return
            self.stats["rx"] += 1
            self.last_rx = now  # only a PONG answers a ping: commands coming in say nothing about the uplink
            for frame in self.parser.feed(data):
                self._packet(frame)
                if self.sock is None:
                    return
        if self.owner and self.term.out:
            while self.term.out and len(self.tx) < TX_CAP:
                chunk = bytes(self.term.out[:FRAME])
                self.term.out = self.term.out[FRAME:]
                self._repl_send(OUT, chunk)
            if self.term.dropped and len(self.tx) < TX_CAP:
                self._repl_send(OUT, ("\r\n[... %d output bytes dropped ...]\r\n" % self.term.dropped).encode())
                self.term.dropped = 0
        if self.fs is not None:
            self._fs_pump()
        if not self.ready and time.ticks_diff(now, self.opened_at) > HELLO_MS:
            self._drop("no ready in time")
            return
        if self.tx and time.ticks_diff(now, self.tx_moved_at) > STALL_MS:
            self._drop("send stall, %d bytes waiting" % len(self.tx))
            return
        if self.ping_at and not self.ping_queued and time.ticks_diff(now, self.ping_at) > PONG_MS:
            self._drop("no reply to ping")
            return
        if not self.ping_at and self.ready and (
                time.ticks_diff(now, self.last_rx) > PING_MS
                or (self.sent_since_pong and time.ticks_diff(now, self.pong_at) > ACTIVE_PING_MS)):
            self.ping_at = now
            self.ping_queued = True
            self._send(bytes([IOMsg.PING]), False, ping=True)
        self._flush()

    # ---- connection

    # ---- bringing the connection up, one small step per tick (nothing here blocks for long)

    def _bring_up(self, now):
        if self.wifi:
            if self.wlan is None:
                import network
                self.wlan = network.WLAN(network.STA_IF)
                self.wlan.active(True)
                self._wifi_tune(self.wlan.isconnected())  # also when Wi-Fi is still up from before (a soft reset)
            if not self.wlan.isconnected():
                self.wifi_tuned = False
                if not self.wifi_at or time.ticks_diff(now, self.wifi_at) > 20000:
                    self.wifi_at = now
                    self.stats["wifi"] = self.stats.get("wifi", 0) + 1
                    try:
                        self.wlan.disconnect()
                    except OSError:
                        pass
                    self._wifi_tune()
                    try:
                        self.wlan.connect(self.wifi[0], self.wifi[1])
                    except OSError as e:
                        self._error("wifi %s" % e)
                return
            self.wifi_at = 0
            if not self.wifi_tuned:  # ESP-IDF puts the send power back to the most when it joins: set it again
                self.wifi_tuned = True
                self._wifi_tune(True)
        if not self.clock_ok:
            if time.ticks_diff(now, self.clock_try) < 0:
                return
            try:
                import ntptime
                ntptime.settime()  # Boho clocks must be within the server's skew (60 s)
                self.clock_ok = True
            except Exception as e:  # noqa: BLE001
                self._error("ntp %s" % e)
                self.clock_try = time.ticks_add(now, 5000)
                return
        if self.pending is None:
            if time.ticks_diff(now, self.next_try) >= 0:
                self._connect_start(now)
        else:
            self._connect_check(now)

    def _log(self, text):
        """One line in the link log: UTC time (once the clock is set), the event, the Wi-Fi signal."""
        if not self.linklog:
            return
        try:
            t = time.gmtime()
            rssi = self.wlan.status("rssi") if self.wlan is not None and self.wlan.isconnected() else None
            line = "%04d-%02d-%02d %02d:%02d:%02dZ %s, rssi %s\n" % (t[0], t[1], t[2], t[3], t[4], t[5], text,
                                                               "-" if rssi is None else rssi)
            try:
                size = os.stat(self.linklog)[6]
            except OSError:
                size = 0
            if size > LINKLOG_MAX:  # keep the newer half
                with open(self.linklog) as f:
                    keep = f.read()[-(LINKLOG_MAX // 2):]
                keep = keep[keep.find("\n") + 1:]
                with open(self.linklog, "w") as f:
                    f.write(keep)
            with open(self.linklog, "a") as f:
                f.write(line)
        except Exception as e:  # noqa: BLE001 - a full or read-only file system must not stop the link
            self._error("linklog %s" % e)

    def _wifi_tune(self, connected=False):
        """Power save off (it delays incoming signals 100-800 ms, as on the Arduino boards) and the send power
        from the config: small ESP32-C3 boards such as iris lose sent packets at full power until TCP stalls.
        Before connecting also the 802.11 modes from the config (changing them while connected drops Wi-Fi)."""
        if self.wifi_mode and not connected:
            try:
                import network
                bits = 0
                for c, m in (("b", "MODE_11B"), ("g", "MODE_11G"), ("n", "MODE_11N")):
                    if c in self.wifi_mode:
                        bits |= getattr(network, m)
                if bits and self.wlan.config("protocol") != bits:
                    self.wlan.config(protocol=bits)
            except Exception as e:  # noqa: BLE001 - firmware without it: keep the chip's modes
                self._error("wifi mode %s" % e)
        try:
            self.wlan.config(pm=self.wlan.PM_NONE)
        except Exception as e:  # noqa: BLE001 - older firmware: keep going
            self._error("wifi pm %s" % e)
        if self.txpower:
            try:
                self.wlan.config(txpower=self.txpower)
            except Exception as e:  # noqa: BLE001
                self._error("wifi txpower %s" % e)

    def _retry(self, now):
        self.next_try = time.ticks_add(now, self.backoff)
        self.backoff = min(self.backoff * 2, 30000)

    def _connect_start(self, now):
        self.stats["connects"] += 1
        try:
            if self.addr is None:
                self.addr = socket.getaddrinfo(self.target[0], self.target[1])[0][-1]
            s = socket.socket()
            s.setblocking(False)
            try:
                s.connect(self.addr)
            except OSError as e:
                if e.args[0] not in (115, 119):  # EINPROGRESS, EALREADY: the connect goes on
                    s.close()
                    raise
        except OSError as e:
            self._error("connect %s" % e)
            self.addr = None
            self._retry(now)
            return
        self.pending = (s, now)

    def _connect_check(self, now):
        import select
        s, started = self.pending
        p = select.poll()
        p.register(s, select.POLLOUT)
        ev = p.poll(0)
        flags = ev[0][1] if ev else 0
        if flags & (select.POLLERR | select.POLLHUP):
            self._error("connect refused")
        elif flags & select.POLLOUT:
            self.pending = None
            self._online(s, now)
            return
        elif time.ticks_diff(now, started) < 5000:
            return
        else:
            self._error("connect timeout")
        self.pending = None
        try:
            s.close()
        except OSError:
            pass
        self.addr = None
        self._retry(now)

    def _online(self, s, now):
        self.sock = s
        self.parser = cong.Parser(65536)
        self.tx = b""
        self.boho = None
        if self.auth:
            self.boho = Boho()
            self.boho.set_id8(self.auth[0])
            self.boho.set_key(self.auth[1])
        self.opened_at = self.last_rx = self.pong_at = now
        self.ping_at = 0
        self.ping_queued = self.sent_since_pong = False
        self.tx_moved_at = now

    # ---- announcing the board (device rules: #search, $name, $ui, $state)

    def _publish(self, tag, text):
        self._send(signal_packet(tag, *encode_payload(text)))

    def _subscribe(self, tag):
        encoded = encode_tag(tag)
        self._send(bytes([IOMsg.SUBSCRIBE, len(encoded)]) + encoded)

    def set_device(self, device):
        """Makes the board this device (mdevice.Device): announced now if online, and on every connect."""
        self.device = device
        device.link = self
        device.repl = self.base is not None
        if self.ready:
            self._announce()

    def subscribe(self, tag, fn):
        """Calls ``fn(tag, value)`` for signals on ``tag`` (value: str for TEXT, bytes for BINARY, "" for EMPTY),
        e.g. ``"abc-123@$state"``. Subscribed now if online and again after every reconnect. Runs in the link's
        timer: keep it short. A retained tag ($state, $name) arrives once right after subscribing."""
        first = tag not in self.listeners
        self.listeners.setdefault(tag, []).append(fn)
        if first and self.ready:
            self._subscribe(tag)

    def signal(self, tag, text=""):
        """Sends a TEXT signal, e.g. ``signal("abc-123@", "r1=1")``; False when not online."""
        if not self.ready:
            return False
        self._publish(tag, text)
        return True

    def device_publish(self, tag, text):
        if self.announce and self.ready:
            self._publish(tag, text)

    def _publish_state(self):
        if not (self.announce and self.ready) or self.device is not None:
            return
        import gc
        v = sys.implementation.version
        self._publish("@$state", "repl=%s;mem=%d;ver=%d.%d.%d" % (
            "open" if self.owner else "idle", gc.mem_free(), v[0], v[1], v[2]))

    def _announce(self):
        if not self.announce:
            return
        self._subscribe("#search")
        if self.device is not None:
            # logged in without a configured name: no $name, so the registered name the server keeps stays
            self.device.announce(self._publish, None if self.auth else "MicroPython")
        else:
            # $name is required by the device rules: apps (and finders) leave out a device without one. A logged-in
            # board without a configured name keeps the registered name the server stores for its CID.
            if self.name or not self.auth:
                self._publish("@$name", self.name or "MicroPython")
            self._publish("@$ui", "t=micropython.1;r=1" if self.base is not None else "t=micropython.1")
            self._publish_state()
        self._publish("#notify", self.cid)
        for tag in self.listeners:
            self._subscribe(tag)

    def _drop(self, why):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self._error("drop: " + why)
            if why not in ("redirect", "stopped", "auth clear"):
                up = ("online %ds" % (time.ticks_diff(time.ticks_ms(), self.online_at) // 1000)) if self.ready and self.online_at is not None else "not ready"
                self._log("drop %s, %s" % (why, up))
                self.down_at = time.ticks_ms()
        self.sock = None
        self.ready = False
        self.cid = ""
        self._end_session()
        self._retry(time.ticks_ms())

    def _send(self, packet, encrypt=True, ping=False):
        if encrypt and self.boho is not None and self.boho.is_authorized:
            packet = self.boho.encrypt_session(packet)
        if not self.tx:
            self.tx_moved_at = time.ticks_ms()  # the stall clock starts with the first waiting byte
        if not ping and packet != PONG_PACKET:
            self.sent_since_pong = True
        self.tx += cong.pack(packet)

    def _flush(self):
        if not self.tx or self.sock is None:
            return
        try:
            n = self.sock.send(self.tx)
        except OSError as e:
            if e.args[0] in (11, 119):
                return
            self._error("send %s" % e)
            self._drop("send")
            return
        if n:
            self.stats["tx"] += 1
            self.tx = self.tx[n:]
            self.tx_moved_at = time.ticks_ms()
            if self.ping_queued and not self.tx:  # the PING has left: start waiting for its PONG now
                self.ping_queued = False
                self.ping_at = self.tx_moved_at

    # ---- packets

    def _packet(self, p):
        if not p:
            return
        if p[0] == IOMsg.ENC:
            p = self.boho.decrypt_session(p) if self.boho else None
            if not p:
                return
        t = p[0]
        if t == IOMsg.SERVER_TIME_NONCE:
            if self.boho is None:
                self._send(bytes([IOMsg.CID_REQ]), False)
            else:
                req = self.boho.auth_req(p)
                if req is not None:
                    self._send(req, False)
        elif t == IOMsg.AUTH_RES:
            if self.boho is not None and self.boho.verify_auth_res(p):
                self._send(bytes([IOMsg.CID_REQ]), False)
            else:
                self._drop("login refused")
        elif t == IOMsg.AUTH_FAIL:
            self._drop("login refused")
        elif t == IOMsg.CID_RES:
            self.cid = p[1:].decode()
            self.ready = True
            self.backoff = 1000
            self.online_at = time.ticks_ms()
            if not self.logged_start:
                self.logged_start = True
                self._log("online %s (start)" % self.cid)
            elif self.down_at is not None:
                self._log("online again after %ds" % (time.ticks_diff(self.online_at, self.down_at) // 1000))
            self.down_at = None
            self._announce()
        elif t == IOMsg.SIGNAL:
            s = decode_signal(p)
            if s is not None and s.tag == "@repl" and s.type == 2:  # BINARY: a sealed frame
                self._repl(s.data)
            elif s is not None and s.tag == "#search" and self.announce:
                self._publish("#notify", self.cid)
            elif s is not None and s.tag == "@" and self.device is not None and s.type == 1:  # TEXT command
                self.device.command(s.data.decode())
            elif s is not None and s.tag in self.listeners:
                value = s.data.decode() if s.type == 1 else s.data if s.type == 2 else ""
                for fn in self.listeners[s.tag]:
                    try:
                        fn(s.tag, value)
                    except Exception as e:  # noqa: BLE001 - the board's code must not stop the link
                        self._error("listener %s: %s" % (s.tag, e))
        elif t == IOMsg.PING:
            self._send(PONG_PACKET, False)
        elif t == IOMsg.PONG:
            self.ping_at = 0
            self.ping_queued = False
            self.pong_at = time.ticks_ms()
            self.sent_since_pong = False
        elif t == IOMsg.SERVER_REDIRECT:
            hp = cong.parse_redirect(p)
            if hp:
                self.target = hp
                self.addr = None
                self.backoff = 0
                self._drop("redirect")
        elif t == IOMsg.AUTH_CLEAR:
            self.auth = None
            self._drop("auth clear")

    def _input(self, data):
        """Controller input. A Ctrl-C is raised at the end of this tick; what follows it waits for the next
        tick, so it reaches the REPL after the interrupt (e.g. Ctrl-C then Ctrl-A for raw REPL)."""
        if self.intr:
            self.after += data
            return
        at = data.find(b"\x03")
        if at < 0:
            self.term.feed(data)
            return
        if at:
            self.term.feed(data[:at])
        self.intr = True
        self.after = data[at + 1:]

    def _repl_send(self, kind, data=b"", sealer=None):
        """Sends a sealed frame to the session owner (the session key unless ``sealer`` is given)."""
        if not self.owner:
            return
        self.tx_seq += 1
        tag = self.owner + "@repl"
        encode_tag(tag)
        frame = (sealer or self.session).seal(kind, self.tx_seq, data)
        self._send(signal_packet(tag, *encode_payload(frame)))

    def _end_session(self):
        self._fs_abort()
        self.owner = None
        self.session = None
        self.term.active = False
        self.term.inbuf = bytearray()
        self.after = b""

    def _repl(self, packet):
        if self.base is None:  # remote REPL off
            self.stats["refused"] += 1
            return
        """A sealed frame from a controller. Anything that does not open with the session key (or, for a
        new session, the REPL key) or repeats an old sequence number is dropped without a reply."""
        if self.session is not None:
            frame = self.session.open(packet)
            if frame is not None:
                kind, seq, data = frame
                if seq <= self.rx_seq:
                    self.stats["replayed"] += 1
                    return
                self.rx_seq = seq
                if kind == IN:
                    self._input(data)
                elif kind == DATA:
                    self._data(data)
                elif kind == FS:
                    self._fs(data)
                elif kind == CLOSE:
                    self._repl_send(CLOSED, b"close")
                    self._end_session()
                    self._publish_state()
                return
        frame = self.base.open(packet)
        if frame is None or frame[0] != OPEN:
            self.stats["refused"] += 1
            return
        try:
            req = json.loads(frame[2])
            reply, cnonce, ts = req["reply"], from_hex(req["cnonce"]), int(req["ts"])
        except (ValueError, KeyError, TypeError):
            self.stats["refused"] += 1
            return
        now = _now_s()
        for old in [c for c, t in self.seen_opens.items() if now - t > OPEN_MAX_AGE_S]:
            del self.seen_opens[old]
        if len(cnonce) != 16 or abs(now - ts) > OPEN_MAX_AGE_S or cnonce in self.seen_opens:
            self.stats["refused"] += 1  # stale or replayed open
            return
        self.seen_opens[cnonce] = now
        if self.owner:
            self._repl_send(CLOSED, b"taken")
        self._end_session()
        a_pub = req.get("a")
        b_pub = None
        bnonce = os.urandom(16)
        pair = (self.eph or self._new_eph()) if a_pub else None
        self.eph = None if pair else self.eph
        if a_pub and pair:  # stage 2: one-time X25519 keys, the secret is dropped right after
            try:
                a_pub = from_hex(a_pub)
                b_sec, b_pub = pair
                del pair
                from .x25519 import shared as _x
                skey = session_key2(self.key, cnonce, bnonce, a_pub, b_pub, _x(b_sec, a_pub))
                del b_sec
            except (ValueError, TypeError):
                self.stats["refused"] += 1
                return
        else:
            skey = session_key(self.key, cnonce, bnonce)
        self.owner = reply
        self.tx_seq = self.rx_seq = 0
        self.session = Sealer(skey)
        del skey
        self.term.active = True
        import gc
        gc.collect()
        info = {"mpy": "%d.%d.%d" % sys.implementation.version[:3], "machine": os.uname().machine,
                "mem_free": gc.mem_free(), "mode": "timer", "timer": self.timer_id}
        body = {"v": 2 if b_pub else 1, "cnonce": to_hex(cnonce), "bnonce": to_hex(bnonce), "info": info}
        if b_pub:
            body["b"] = to_hex(b_pub)
        self._repl_send(OPENED, json.dumps(body).encode(), self.base)
        self._publish_state()


    @staticmethod
    def _new_eph():
        try:
            from .x25519 import public
        except ImportError:
            return False
        secret = os.urandom(32)
        return secret, public(secret)

    # ---- files (not through the REPL)

    def _fs_reply(self, body):
        self._repl_send(FS_RES, json.dumps(body).encode())

    def _fs_abort(self):
        fs, self.fs = self.fs, None
        if not fs:
            return
        try:
            fs["f"].close()
        except OSError:
            pass
        if fs["mode"] == "put":
            try:
                os.remove(fs["path"] + ".part")
            except OSError:
                pass

    def _info(self):
        """Memory, file space and the link's own CPU share since the last call, for the web IDE's usage panel.
        A few ms (one gc.collect); the IDE asks every 30 s at most."""
        import gc
        gc.collect()
        now = time.ticks_ms()
        window = max(1, time.ticks_diff(now, self.busy_since))
        cpu = self.busy_us / (10 * window)  # % of the window spent in _tick
        self.busy_us, self.busy_since = 0, now
        st = os.statvfs("/")
        body = {"op": "info", "ok": True, "heap": [gc.mem_alloc(), gc.mem_free()], "fs": [st[0] * st[2], st[0] * st[3]],
                "cpu": int(cpu * 10) / 10, "window_ms": window}
        try:
            import esp32
            r = esp32.idf_heap_info(esp32.HEAP_DATA)
            body["idf"] = [sum(x[0] for x in r), sum(x[1] for x in r), sum(x[3] for x in r)]
        except Exception:  # noqa: BLE001 - not an ESP32 (unix port): no system heap figures
            pass
        return body

    def _fs(self, raw):
        try:
            req = json.loads(raw)
            op = req.get("op")
        except ValueError:
            return
        try:
            if op == "put":
                self._fs_abort()
                path = req["path"]
                self.fs = {"mode": "put", "path": path, "f": open(path + ".part", "wb"), "at": 0, "acked": 0,
                           "size": int(req["size"]), "h": hashlib.sha256()}
                self._fs_reply({"op": "put", "ok": True})
            elif op == "close":
                fs = self.fs
                if not fs or fs["mode"] != "put":
                    raise OSError("no upload in progress")
                self.fs = None
                fs["f"].close()
                digest = to_hex(fs["h"].digest())
                ok = fs["at"] == fs["size"] and digest == req.get("sha256")
                part = fs["path"] + ".part"
                if ok:
                    try:
                        os.remove(fs["path"])
                    except OSError:
                        pass
                    os.rename(part, fs["path"])
                else:
                    os.remove(part)
                self._fs_reply({"op": "close", "ok": ok, "size": fs["at"], "sha256": digest})
            elif op == "get":
                self._fs_abort()
                path = req["path"]
                size = os.stat(path)[6]
                self.fs = {"mode": "get", "path": path, "f": open(path, "rb"), "at": 0, "size": size,
                           "h": hashlib.sha256()}
                self._fs_reply({"op": "get", "ok": True, "size": size})
            elif op == "abort":
                self._fs_abort()
                self._fs_reply({"op": "abort", "ok": True})
            elif op == "ls":
                items = []
                for e in os.ilistdir(req.get("path", "/")):
                    items.append([e[0], "d" if e[1] == 0x4000 else "f", e[3] if len(e) > 3 else 0])
                self._fs_reply({"op": "ls", "ok": True, "items": items})
            elif op == "stat":
                st = os.stat(req["path"])
                self._fs_reply({"op": "stat", "ok": True, "dir": st[0] & 0x4000 != 0, "size": st[6]})
            elif op in ("rm", "mkdir", "rmdir"):
                (os.remove if op == "rm" else os.mkdir if op == "mkdir" else os.rmdir)(req["path"])
                self._fs_reply({"op": op, "ok": True})
            elif op == "rename":
                os.rename(req["path"], req["to"])
                self._fs_reply({"op": op, "ok": True})
            elif op == "info":
                self._fs_reply(self._info())
            else:
                self._fs_reply({"op": op, "ok": False, "error": "unknown op"})
        except (OSError, KeyError, ValueError, TypeError) as e:
            if op in ("put", "get"):
                self._fs_abort()
            self._fs_reply({"op": op, "ok": False, "error": "%s: %s" % (type(e).__name__, e)})

    def _data(self, data):
        fs = self.fs
        if not fs or fs["mode"] != "put" or len(data) < 4:
            return
        offset = int.from_bytes(data[:4], "little")
        if offset != fs["at"]:
            self._fs_abort()
            self._fs_reply({"op": "put", "ok": False, "error": "offset %d, expected %d" % (offset, fs["at"])})
            return
        chunk = data[4:]
        fs["f"].write(chunk)
        fs["h"].update(chunk)
        fs["at"] += len(chunk)

    def _fs_pump(self):
        fs = self.fs
        if fs["mode"] == "put":
            if fs["at"] - fs["acked"] >= FS_ACK_EVERY or (fs["at"] == fs["size"] and fs["acked"] != fs["at"]):
                fs["acked"] = fs["at"]
                self._fs_reply({"op": "ack", "at": fs["at"]})
            return
        for _ in range(GET_PER_TICK):
            if len(self.tx) > TX_CAP // 2:
                return
            chunk = fs["f"].read(GET_CHUNK)
            if not chunk:
                self.fs = None
                fs["f"].close()
                self._fs_reply({"op": "end", "size": fs["at"], "sha256": to_hex(fs["h"].digest())})
                return
            fs["h"].update(chunk)
            self._repl_send(DATA, fs["at"].to_bytes(4, "little") + chunk)
            fs["at"] += len(chunk)


def _now_s():
    from .boho import _now_ms
    return _now_ms() // 1000


link = None


def last_timer():
    """The highest hardware timer number this chip has (ESP32, S3: 3; C3, C6: 1), 0 if none answers.
    Making a Timer object does not start or change the timer."""
    import machine
    for n in (3, 2, 1):
        try:
            machine.Timer(n)
            return n
        except Exception:  # noqa: BLE001 - ValueError on ports without that timer
            pass
    return 0


def start(url, key, auth=None, period_ms=20, wifi=None, name=None, announce=True, txpower=None, wifi_mode=None,
          linklog=True, timer=None):
    """Starts the link and returns at once; the REPL keeps the main thread. ``key``: the REPL key
    (bytes, 32 recommended). None: no remote REPL (no dupterm, @repl refused), the board can still be a device."""
    global link
    link = ReplLink(url, key, auth, period_ms, wifi, name, announce, txpower, wifi_mode, linklog,
                    timer).start()
    return link
