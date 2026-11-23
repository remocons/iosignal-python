"""Sealed REPL frames (``repl.1``, stage 1 of the remote REPL design).

Every ``<cid>@repl`` payload is a Boho ENC_PACK (authenticated, 8-byte tag) of ``[type][seq u32 LE][data]``,
so the broker relays only sealed bytes. Keys:

- the REPL key (32 bytes) is shared by the board and its controller, set over USB, never sent through a server;
- ``open`` and ``opened`` are sealed with the REPL key; everything after with the session key
  ``sha256("iosignal-repl/1" | key | cnonce | bnonce)`` (controller nonce, board nonce), so frames recorded
  in one session do not open in another;
- ``seq`` counts from 1 in each direction; a receiver drops a frame whose seq is not above the last one.
- stage 2 (forward secrecy, 2026-11-23): ``open`` also carries ``"a"`` (the controller's one-time X25519 public
  key) and ``opened`` answers ``"b"`` (the board's); both are sealed with the REPL key, so neither can be swapped.
  The session key is then ``sha256("iosignal-repl/2" | X25519 shared | key | cnonce | bnonce | A | B)`` and both
  sides drop their X25519 secrets: frames recorded today stay closed even if the REPL key leaks later. A board
  without stage 2 answers without ``"b"`` and both sides use the stage 1 key.

``open`` data: JSON ``{"v":1,"reply":"<controller CID>","cnonce":"<32 hex>","ts":<unix seconds>}``.
``opened`` data: JSON ``{"v":1,"cnonce":…,"bnonce":"<32 hex>","info":{…}}``.

Files (session key, same sequence numbers): ``FS`` requests ``{"op": put|close|get|abort|ls|stat|rm|mkdir|rmdir|
rename, ...}``; ``put`` writes ``<path>.part`` and ``close`` (with ``sha256``) renames it only when size and hash
match; ``get`` streams DATA then an ``end`` reply with the hash. The board acks ``put`` data with
``{"op":"ack","at":<bytes written>}`` so the controller can keep a window in flight.
Runs on CPython 3.10+ and MicroPython 1.28+.
"""

from .boho import Boho, sha256

OPEN, OPENED, IN, OUT, CLOSE, CLOSED = 1, 2, 3, 4, 5, 6
# Files, handled by the board's link itself (not through the REPL): FS request (JSON), FS_RES reply (JSON),
# DATA ``[offset u32 LE][bytes]`` in either direction. See replpoll.ReplLink._fs.
FS, FS_RES, DATA = 7, 8, 9
LABEL = b"iosignal-repl/1"
LABEL2 = b"iosignal-repl/2"
OPEN_MAX_AGE_S = 120

__all__ = ["OPEN", "OPENED", "IN", "OUT", "CLOSE", "CLOSED", "FS", "FS_RES", "DATA", "Sealer", "session_key", "session_key2", "to_hex",
           "from_hex"]


def session_key(key: bytes, cnonce: bytes, bnonce: bytes) -> bytes:
    return sha256(LABEL, key, cnonce, bnonce)


def session_key2(key, cnonce, bnonce, a_pub, b_pub, shared):
    """Stage 2: also bound to both one-time X25519 keys and their shared secret."""
    return sha256(LABEL2, shared, key, cnonce, bnonce, a_pub, b_pub)


def to_hex(b: bytes) -> str:
    return "".join("%02x" % x for x in b)


def from_hex(s: str) -> bytes:
    return bytes(int(s[i:i + 2], 16) for i in range(0, len(s), 2))


class Sealer:
    """Seals and opens frames with one key (the REPL key or a session key)."""

    def __init__(self, key: bytes):
        if len(key) < 16:
            raise ValueError("REPL key too short")
        self.key = bytes(key)
        self._boho = Boho()
        self._boho.copy_key(sha256(self.key))

    def seal(self, kind: int, seq: int, data: bytes = b"") -> bytes:
        return self._boho.encrypt_pack(bytes([kind]) + seq.to_bytes(4, "little") + data)

    def open(self, packet: bytes):
        """(kind, seq, data), or None when the packet is not sealed with this key or is damaged."""
        plain = self._boho.decrypt_pack(packet)
        if plain is None or len(plain) < 5:
            return None
        return plain[0], int.from_bytes(plain[1:5], "little"), plain[5:]
