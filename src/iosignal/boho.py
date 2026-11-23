"""Boho 2.x session protocol (npm ``boho``, src/boho.js): login proof, ENC_488 session packets and ENC_PACK.

Byte layouts and hash inputs match the JavaScript implementation. Both sides are here: the client
(``auth_req``, ``verify_auth_res``) and the server (``server_time_nonce``, ``verify_auth_req``), so the
protocol can be tested without a server.

Packets:
  SERVER_TIME_NONCE  B0 | u32LE seconds | u16LE ms | u16LE counter | nonce4            13 bytes
  AUTH_REQ           B1 | id8 | nonce4 | sha256(key32 | salt12 | nonce4)             45 bytes
  AUTH_RES           B2 | sha256(key32 | salt12' | clientNonce)                       33 bytes
  ENC_488            B8 | u32LE len | clock8 | tag8 | xdata                           21 + len
  ENC_PACK           B6 | u32LE len | salt12 | tag8 | xdata                           25 + len

``key32`` is sha256(key). Tags are the legacy prefix hash sha256(key32 | salt12 | data), not HMAC.
"""

import hashlib
import os
import time

try:
    import _thread
except ImportError:  # a MicroPython build without threads
    _thread = None

__all__ = [
    "Boho", "SERVER_TIME_NONCE", "AUTH_REQ", "AUTH_RES", "AUTH_FAIL", "ENC_PACK", "ENC_E2E", "ENC_488",
    "SERVER_TIME_NONCE_SIZE", "AUTH_REQ_SIZE", "AUTH_RES_SIZE", "ENC_PACK_META", "ENC_488_META", "sha256",
]

SERVER_TIME_NONCE = 0xB0
AUTH_REQ = 0xB1
AUTH_RES = 0xB2
AUTH_FAIL = 0xB3
ENC_PACK = 0xB6
ENC_E2E = 0xB7
ENC_488 = 0xB8

SERVER_TIME_NONCE_SIZE = 13
AUTH_REQ_SIZE = 45
AUTH_RES_SIZE = 33
ENC_PACK_META = 25
ENC_488_META = 21



def _equal(a: bytes, b: bytes) -> bool:
    """Compares tags without an early exit on the first difference (hmac.compare_digest is not on MicroPython)."""
    if len(a) != len(b):
        return False
    diff = 0
    for x, y in zip(a, b):
        diff |= x ^ y
    return diff == 0


class _RLock:
    """Reentrant lock on _thread (threading.RLock is not on MicroPython). A no-op without threads."""

    def __init__(self):
        self._lock = _thread.allocate_lock() if _thread else None
        self._owner = None
        self._depth = 0

    def __enter__(self):
        if self._lock is None:
            return self
        me = _thread.get_ident()
        if self._owner == me:
            self._depth += 1
            return self
        self._lock.acquire()
        self._owner, self._depth = me, 1
        return self

    def __exit__(self, *exc):
        if self._lock is None:
            return
        self._depth -= 1
        if self._depth == 0:
            self._owner = None
            self._lock.release()


def sha256(*parts: bytes) -> bytes:
    h = hashlib.sha256()
    for p in parts:
        h.update(p)
    return h.digest()


def _bytes(data) -> bytes:
    return data.encode() if isinstance(data, str) else bytes(data)


# Some MicroPython ports count from 2000-01-01; Boho clocks are Unix epoch milliseconds.
_EPOCH_OFFSET_MS = 946684800000 if time.gmtime(0)[0] == 2000 else 0


def _now_ms() -> int:
    """Unix epoch milliseconds. (MicroPython boards need the clock set, e.g. by ntptime: the server
    refuses encrypted packets whose clock is more than 60 s off.)"""
    if hasattr(time, "time_ns"):
        return time.time_ns() // 1000000 + _EPOCH_OFFSET_MS
    return int(time.time() * 1000) + _EPOCH_OFFSET_MS


class Boho:
    """One Boho session. Senders and the socket reader may share it, so state changes hold a lock.

    ``rand(n)`` and ``clock()`` (epoch milliseconds) can be replaced in tests.
    """

    def __init__(self, rand=os.urandom, clock=_now_ms):
        self._rand = rand
        self._clock = clock
        self._lock = _RLock()
        self.clear_auth()

    # ---- key and ID

    def clear_auth(self) -> None:
        with self._lock:
            self._id8 = bytes(8)
            self._key32 = bytes(32)
            self._has_key = False
            self._salt12 = bytes(12)
            self._auth_salt12 = bytes(12)
            self.local_nonce = bytes(4)
            self.remote_nonce = bytes(4)
            self.counter = 0
            self._last_send_time = 0
            self.is_authorized = False

    def set_id8(self, id_) -> None:
        """The ID's first 8 UTF-8 bytes, zero padded."""
        raw = _bytes(id_)[:8]
        self._id8 = raw + bytes(8 - len(raw))

    def set_key(self, key) -> None:
        data = _bytes(key)
        if not data:
            raise ValueError("Key must not be empty")
        self.copy_key(sha256(data))

    def copy_key(self, key32: bytes) -> None:
        if len(key32) != 32:
            raise ValueError("copy_key requires exactly 32 bytes")
        with self._lock:
            self._key32 = bytes(key32)
            self._has_key = True

    def set_id_key(self, id_key: str) -> bool:
        """Splits ``id.key`` at the first dot. False (nothing set) without a dot."""
        if "." not in id_key:
            return False
        id_, key = id_key.split(".", 1)
        self.set_id8(id_)
        self.set_key(key)
        return True

    @property
    def has_key(self) -> bool:
        return self._has_key

    # ---- primitives

    def _require_key(self) -> None:
        if not self._has_key:
            raise RuntimeError("Set a key before encryption or authentication")

    def _tag(self, data: bytes) -> bytes:
        return sha256(self._key32, self._salt12, data)

    def _xotp(self, data: bytes) -> bytes:
        """XOR with sha256(otp32 | u32LE index) blocks, index from 1, otp32 = sha256(key32 | salt12)."""
        otp32 = sha256(self._key32, self._salt12)
        out = bytearray(data)
        for block, offset in enumerate(range(0, len(out), 32), 1):
            pad = sha256(otp32, block.to_bytes(4, "little"))
            for i in range(min(32, len(out) - offset)):
                out[offset + i] ^= pad[i]
        return bytes(out)

    def _set_clock_nonce(self, nonce: bytes) -> None:
        """A monotonic clock and counter, with the peer's nonce, as the salt."""
        now = max(self._clock(), self._last_send_time)
        self.counter = (self.counter + 1) & 0xFFFF
        if self.counter == 0 and now == self._last_send_time:
            now += 1
        if now < 0 or now // 1000 > 0xFFFFFFFF:
            raise ValueError("Clock is outside the protocol range")
        self._last_send_time = now
        self._salt12 = ((now // 1000).to_bytes(4, "little") + (now % 1000).to_bytes(2, "little")
                        + self.counter.to_bytes(2, "little") + nonce)

    @staticmethod
    def _sized(packet: bytes, type_: int, meta: int, variable: bool) -> bool:
        if len(packet) < meta or packet[0] != type_:
            return False
        if variable:
            return int.from_bytes(packet[1:5], "little") == len(packet) - meta
        return len(packet) == meta

    # ---- login: client

    def auth_req(self, challenge: bytes) -> bytes | None:
        """AUTH_REQ answering a SERVER_TIME_NONCE challenge; None without a key or for a malformed challenge."""
        with self._lock:
            if not self._has_key or not self._sized(challenge, SERVER_TIME_NONCE, SERVER_TIME_NONCE_SIZE, False):
                return None
            if int.from_bytes(challenge[5:7], "little") >= 1000:
                return None
            self.is_authorized = False
            self._salt12 = bytes(challenge[1:13])
            self.local_nonce = self._rand(4)
            self.remote_nonce = bytes(challenge[9:13])
            return bytes([AUTH_REQ]) + self._id8 + self.local_nonce + self._tag(self.local_nonce)

    def verify_auth_res(self, packet: bytes) -> bool:
        """True when AUTH_RES proves the server knows the key; the session is then authorized."""
        with self._lock:
            if not self._has_key or not self._sized(packet, AUTH_RES, AUTH_RES_SIZE, False):
                return False
            self._salt12 = self.remote_nonce + self.local_nonce + self.remote_nonce
            ok = _equal(self._tag(self.local_nonce), bytes(packet[1:33]))
            if ok:
                self.is_authorized = True
            return ok

    # ---- login: server

    def server_time_nonce(self) -> bytes:
        with self._lock:
            now = self._clock()
            self.local_nonce = self._rand(4)
            self._auth_salt12 = ((now // 1000).to_bytes(4, "little") + (now % 1000).to_bytes(2, "little")
                                 + self._rand(2) + self.local_nonce)
            return bytes([SERVER_TIME_NONCE]) + self._auth_salt12

    def id8_of(self, packet: bytes) -> bytes | None:
        """The ID field of an AUTH_REQ, so a server can look up the key before verifying."""
        return bytes(packet[1:9]) if self._sized(packet, AUTH_REQ, AUTH_REQ_SIZE, False) else None

    def verify_auth_req(self, packet: bytes) -> bytes | None:
        """AUTH_RES for a valid AUTH_REQ (the session is then authorized), else None."""
        with self._lock:
            if not self._has_key or not self._sized(packet, AUTH_REQ, AUTH_REQ_SIZE, False):
                return None
            nonce = bytes(packet[9:13])
            self._salt12 = self._auth_salt12
            if not _equal(self._tag(nonce), bytes(packet[13:45])):
                return None
            self.remote_nonce = nonce
            self._salt12 = self.local_nonce + self.remote_nonce + self.local_nonce
            self.is_authorized = True
            return bytes([AUTH_RES]) + self._tag(nonce)

    # ---- session packets

    def deauthorize(self) -> None:
        """A dropped connection needs a new login; the ID and key stay for it."""
        self.is_authorized = False

    def encrypt_488(self, data) -> bytes | None:
        """ENC_488 packet; None before authentication."""
        with self._lock:
            if not self.is_authorized or not self._has_key:
                return None
            plain = _bytes(data)
            self._set_clock_nonce(self.remote_nonce)
            tag = self._tag(plain)
            return (bytes([ENC_488]) + len(plain).to_bytes(4, "little") + self._salt12[:8] + tag[:8]
                    + self._xotp(plain))

    def decrypt_488(self, packet: bytes) -> bytes | None:
        """Plaintext of an ENC_488 packet, or of the encrypted header of an ENC_E2E packet.

        None for a malformed, unauthenticated or forged packet.
        """
        with self._lock:
            if not self.is_authorized or not self._has_key:
                return None
            p = bytes(packet)
            if p[:1] == bytes([ENC_E2E]):
                if len(p) < ENC_488_META:
                    return None
                header_len = int.from_bytes(p[1:5], "little")
                if header_len > len(p) - ENC_488_META:
                    return None
                p = bytes([ENC_488]) + p[1:ENC_488_META + header_len]
            if not self._sized(p, ENC_488, ENC_488_META, True):
                return None
            if int.from_bytes(p[9:11], "little") >= 1000:  # the clock's milliseconds
                return None
            self._salt12 = p[5:13] + self.local_nonce
            plain = self._xotp(p[ENC_488_META:])
            if not _equal(self._tag(plain)[:8], p[13:21]):
                return None
            return plain

    def encrypt_pack(self, data) -> bytes:
        """ENC_PACK with this key and a random nonce; needs no login."""
        with self._lock:
            self._require_key()
            plain = _bytes(data)
            self._set_clock_nonce(self._rand(4))
            tag = self._tag(plain)
            return (bytes([ENC_PACK]) + len(plain).to_bytes(4, "little") + self._salt12 + tag[:8]
                    + self._xotp(plain))

    def decrypt_pack(self, packet: bytes) -> bytes | None:
        with self._lock:
            if not self._has_key or not self._sized(bytes(packet), ENC_PACK, ENC_PACK_META, True):
                return None
            p = bytes(packet)
            self._salt12 = p[5:17]
            plain = self._xotp(p[ENC_PACK_META:])
            if not _equal(self._tag(plain)[:8], p[17:25]):
                return None
            return plain

    def encrypt_e2e(self, data, key) -> bytes:
        """ENC_PACK with another key, leaving the session key and salt as they were."""
        return self._with_key(key, lambda: self.encrypt_pack(data))

    def decrypt_e2e(self, packet: bytes, key) -> bytes | None:
        return self._with_key(key, lambda: self.decrypt_pack(packet))

    def _with_key(self, key, operation):
        k = _bytes(key)
        if not k:
            raise ValueError("Key must not be empty")
        with self._lock:
            saved = (self._key32, self._has_key, self._salt12)
            try:
                self._key32, self._has_key = sha256(k), True
                return operation()
            finally:
                self._key32, self._has_key, self._salt12 = saved
