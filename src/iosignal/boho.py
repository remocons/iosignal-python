"""Boho 3 (npm ``boho`` 3, src/boho.js): shared-key login and encrypted packets on Ascon (NIST SP 800-232).

Byte layouts match the JavaScript implementation (and its shared vectors, test/fixtures/boho-vectors.json).
Both sides are here: the client (``auth_req``, ``verify_auth_res``) and the server
(``server_time_nonce``, ``verify_auth_req``), so the protocol can be tested without a server.

Packets:
  SERVER_TIME_NONCE  B0 | u32LE seconds | u16LE ms | random6                         13 bytes
  AUTH_REQ           B4 | id8 | clientNonce8 | clientProof16                         33 bytes
  AUTH_RES           B5 | serverProof16                                              17 bytes
  ENC / ENC_E2E      BB / BA | u32LE len | sec4 ms2 counter2 | tag8 | ciphertext      21 + len
  ENC_PACK           B9 | u32LE len | nonce16 (sec4 ms2 counter2 sender8) | tag8 | ct 29 + len

Keys: ``derive_key(key) = Ascon-CXOF128("boho/v2/key", key)``, 16 bytes. A login makes
``CXOF128("boho/v2/session", key | challenge | id8 | clientNonce)`` = session key | client proof | server proof.
Session packets: Ascon-AEAD128, nonce = header clock + direction byte (client 1, server 2),
associated data = the 13-byte header. The Boho 2.x code (SHA-256 based) is in classic/.
"""

import hashlib
import os
import time

from . import ascon

try:
    import _thread
except ImportError:  # a MicroPython build without threads
    _thread = None

__all__ = [
    "Boho", "SERVER_TIME_NONCE", "AUTH_REQ", "AUTH_RES", "AUTH_FAIL", "ENC_PACK", "ENC_E2E", "ENC",
    "SERVER_TIME_NONCE_SIZE", "AUTH_REQ_SIZE", "AUTH_RES_SIZE", "ENC_PACK_META", "ENC_META",
    "derive_key", "sha256",
]

SERVER_TIME_NONCE = 0xB0
AUTH_FAIL = 0xB3
AUTH_REQ = 0xB4
AUTH_RES = 0xB5
ENC_PACK = 0xB9
ENC_E2E = 0xBA
ENC = 0xBB

SERVER_TIME_NONCE_SIZE = 13
AUTH_REQ_SIZE = 33
AUTH_RES_SIZE = 17
ENC_META = 21
ENC_PACK_META = 29

_KEY_CUSTOM = b"boho/v2/key"
_SESSION_CUSTOM = b"boho/v2/session"
_TAG = 8
_DIR = {"client": 1, "server": 2}  # the direction byte of what that side sends


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
    """SHA-256 helper (not used by the Boho protocol; the REPL link derives its keys with it)."""
    h = hashlib.sha256()
    for p in parts:
        h.update(p)
    return h.digest()


def _bytes(data) -> bytes:
    return data.encode() if isinstance(data, str) else bytes(data)


def derive_key(key) -> bytes:
    """Key string (or bytes) → the 16-byte key, what a server stores for an ID."""
    data = _bytes(key)
    if not data:
        raise ValueError("Key must not be empty")
    return ascon.cxof128(data, 16, _KEY_CUSTOM)


# Some MicroPython ports count from 2000-01-01; Boho clocks are Unix epoch milliseconds.
_EPOCH_OFFSET_MS = 946684800000 if time.gmtime(0)[0] == 2000 else 0


def _now_ms() -> int:
    """Unix epoch milliseconds. (MicroPython boards need the clock set, e.g. by ntptime: the server
    refuses encrypted packets whose clock is more than 60 s off.)"""
    if hasattr(time, "time_ns"):
        return time.time_ns() // 1000000 + _EPOCH_OFFSET_MS
    return int(time.time() * 1000) + _EPOCH_OFFSET_MS


def _session_nonce(header: bytes, direction: int) -> bytes:
    return bytes(header[5:13]) + bytes([direction]) + bytes(7)


def _valid_challenge(c) -> bool:
    return len(c) == SERVER_TIME_NONCE_SIZE and c[0] == SERVER_TIME_NONCE and int.from_bytes(c[5:7], "little") < 1000


class Boho:
    """One Boho session. Senders and the socket reader may share it, so state changes hold a lock.

    ``rand(n)`` and ``clock()`` (epoch milliseconds) can be replaced in tests.
    """

    def __init__(self, rand=os.urandom, clock=_now_ms):
        self._rand = rand
        self._clock = clock
        self._lock = _RLock()
        self._sender = None
        self.clear_auth()

    # ---- key and ID

    def clear_auth(self) -> None:
        with self._lock:
            self._id8 = bytes(8)
            self._key = None
            self._session_key = None
            self._server_proof = None
            self._challenge = None
            self._role = None
            self._last_ms = -1
            self._last_counter = 0
            self.is_authorized = False

    def set_id8(self, id_) -> None:
        """The ID's first 8 UTF-8 bytes, zero padded."""
        raw = _bytes(id_)[:8]
        self._id8 = raw + bytes(8 - len(raw))

    def set_key(self, key) -> None:
        """A key string; a new key ends the session."""
        self.copy_key(derive_key(key))

    def copy_key(self, key16: bytes) -> None:
        """A stored 16-byte key (``derive_key``)."""
        if len(key16) != 16:
            raise ValueError("copy_key requires exactly 16 bytes")
        with self._lock:
            self._key = bytes(key16)
            self._session_key = None
            self.is_authorized = False

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
        return self._key is not None

    def _session(self, challenge: bytes, id8: bytes, client_nonce: bytes) -> bytes:
        return ascon.cxof128(self._key + bytes(challenge) + bytes(id8) + bytes(client_nonce), 48, _SESSION_CUSTOM)

    # ---- login: client

    def auth_req(self, challenge: bytes, client_nonce: bytes | None = None) -> bytes | None:
        """AUTH_REQ answering a SERVER_TIME_NONCE challenge; None without a key or for a malformed challenge."""
        with self._lock:
            if self._key is None or not _valid_challenge(challenge):
                return None
            nonce = bytes(client_nonce) if client_nonce is not None else self._rand(8)
            out = self._session(challenge, self._id8, nonce)
            self._session_key = out[:16]
            self._server_proof = out[32:48]
            self._role = "client"
            self.is_authorized = False
            return bytes([AUTH_REQ]) + self._id8 + nonce + out[16:32]

    def verify_auth_res(self, packet: bytes) -> bool:
        """True when AUTH_RES proves the server knows the key; the session is then authorized."""
        with self._lock:
            if (self._server_proof is None or len(packet) != AUTH_RES_SIZE or packet[0] != AUTH_RES
                    or not _equal(bytes(packet[1:]), self._server_proof)):
                return False
            self._server_proof = None
            self.is_authorized = True
            return True

    # ---- login: server

    def server_time_nonce(self) -> bytes:
        """The 13-byte challenge, kept for ``verify_auth_req``."""
        with self._lock:
            now = self._clock()
            self._challenge = (bytes([SERVER_TIME_NONCE]) + (now // 1000).to_bytes(4, "little")
                               + (now % 1000).to_bytes(2, "little") + self._rand(6))
            return self._challenge

    def id8_of(self, packet: bytes) -> bytes | None:
        """The ID field of an AUTH_REQ, so a server can look up the key before verifying."""
        return bytes(packet[1:9]) if len(packet) == AUTH_REQ_SIZE and packet[0] == AUTH_REQ else None

    def verify_auth_req(self, packet: bytes, challenge: bytes | None = None) -> bytes | None:
        """AUTH_RES for a valid AUTH_REQ (the session is then authorized), else None."""
        with self._lock:
            challenge = challenge if challenge is not None else self._challenge
            if (self._key is None or challenge is None or not _valid_challenge(challenge)
                    or len(packet) != AUTH_REQ_SIZE or packet[0] != AUTH_REQ):
                return None
            out = self._session(challenge, packet[1:9], packet[9:17])
            if not _equal(bytes(packet[17:33]), out[16:32]):
                return None
            self._id8 = bytes(packet[1:9])
            self._session_key = out[:16]
            self._role = "server"
            self.is_authorized = True
            return bytes([AUTH_RES]) + out[32:48]

    # ---- session packets

    def deauthorize(self) -> None:
        """A dropped connection needs a new login; the ID and key stay for it."""
        self.is_authorized = False

    def _next_clock(self):
        """The next outgoing (seconds, ms, counter): strictly increasing, from the clock."""
        ms = self._clock()
        if ms > self._last_ms:
            counter = (self._last_counter + 1) & 0xFFFF
        else:
            ms = self._last_ms
            counter = self._last_counter + 1
            if counter > 0xFFFF:
                ms += 1
                counter = 0
        if ms < 0 or ms // 1000 > 0xFFFFFFFF:
            raise ValueError("Clock is outside the protocol range")
        self._last_ms, self._last_counter = ms, counter
        return ms // 1000, ms % 1000, counter

    def _header(self, type_: int, length: int) -> bytes:
        sec, ms, counter = self._next_clock()
        return (bytes([type_]) + length.to_bytes(4, "little") + sec.to_bytes(4, "little")
                + ms.to_bytes(2, "little") + counter.to_bytes(2, "little"))

    def encrypt_session(self, data, type_: int = ENC) -> bytes | None:
        """ENC packet, or ENC_E2E for a routing header; None before authentication."""
        with self._lock:
            if not self.is_authorized or self._session_key is None or type_ not in (ENC, ENC_E2E):
                return None
            plain = _bytes(data)
            header = self._header(type_, len(plain))
            cipher, tag = ascon.encrypt(self._session_key, _session_nonce(header, _DIR[self._role]), plain, header, _TAG)
            return header + tag + cipher

    def decrypt_session(self, packet: bytes) -> bytes | None:
        """Plaintext of an ENC packet, or of the encrypted routing header of an ENC_E2E packet.

        None for a malformed, unauthenticated or forged packet.
        """
        with self._lock:
            if not self.is_authorized or self._session_key is None:
                return None
            p = bytes(packet)
            if len(p) < ENC_META or p[0] not in (ENC, ENC_E2E):
                return None
            length = int.from_bytes(p[1:5], "little")
            room = len(p) - ENC_META
            if length > room or (p[0] == ENC and length != room):
                return None
            if int.from_bytes(p[9:11], "little") >= 1000:  # the clock's milliseconds
                return None
            other = _DIR["server"] if self._role == "client" else _DIR["client"]
            header = p[:13]
            return ascon.decrypt(self._session_key, _session_nonce(header, other), p[ENC_META:ENC_META + length],
                                 header, p[13:21])

    def _seal(self, key: bytes, data) -> bytes:
        plain = _bytes(data)
        if self._sender is None:
            self._sender = self._rand(8)
        sec, ms, counter = self._next_clock()
        head = (bytes([ENC_PACK]) + len(plain).to_bytes(4, "little") + sec.to_bytes(4, "little")
                + ms.to_bytes(2, "little") + counter.to_bytes(2, "little") + self._sender)
        cipher, tag = ascon.encrypt(key, head[5:21], plain, head[:5], _TAG)
        return head + tag + cipher

    @staticmethod
    def _open(key: bytes, packet: bytes) -> bytes | None:
        p = bytes(packet)
        if len(p) < ENC_PACK_META or p[0] != ENC_PACK:
            return None
        if int.from_bytes(p[1:5], "little") != len(p) - ENC_PACK_META or int.from_bytes(p[9:11], "little") >= 1000:
            return None
        return ascon.decrypt(key, p[5:21], p[ENC_PACK_META:], p[:5], p[21:29])

    def encrypt_pack(self, data) -> bytes:
        """ENC_PACK with this key; needs no login."""
        with self._lock:
            if self._key is None:
                raise RuntimeError("Set a key before encryption or authentication")
            return self._seal(self._key, data)

    def decrypt_pack(self, packet: bytes) -> bytes | None:
        with self._lock:
            return None if self._key is None else self._open(self._key, packet)

    def encrypt_e2e(self, data, key) -> bytes:
        """ENC_PACK with an E2E key (string or bytes), leaving the session as it was."""
        with self._lock:
            return self._seal(derive_key(key), data)

    def decrypt_e2e(self, packet: bytes, key) -> bytes | None:
        return self._open(derive_key(key), packet)
