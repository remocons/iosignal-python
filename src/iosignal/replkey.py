"""REPL key text → key bytes, the same rule as web/replclient.js (``replKeyBytes``).

    hex, even length, 32 digits or more   → those bytes (the 64-digit keys made before 2026-11-28)
    25 Crockford base32 symbols, plain or as 5-5-5-5-5 with "-"   → 16 bytes (125 random bits, the keys made now)
    anything else, 10 characters or more  → a passphrase: PBKDF2-HMAC-SHA256(NFC text, "iosignal/repl/v1", 600000), 32 bytes

The board stores only the bytes (hex in /repl.json) and never reads key text, so this module is for computers
(CPython): iosignal-repl and tools/mpy_repl_key.py. Shared vectors: tests/fixtures/repl-key-vectors.json.
"""

import hashlib
import os
import re
import unicodedata

SALT = b"iosignal/repl/v1"
ROUNDS = 600000
PASSPHRASE_MIN = 10
_B32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_HEX = re.compile(r"^(?:[0-9a-fA-F]{2}){16,}$")
_B32_PLAIN = re.compile(r"^[0-9A-Z]{25}$")
_B32_GROUPED = re.compile(r"^(?:[0-9A-Z]{5}-){4}[0-9A-Z]{5}$")


def _base32(text: str):
    t = text.upper()
    if not (_B32_PLAIN.match(t) or _B32_GROUPED.match(t)):
        return None
    n = 0
    for ch in t.replace("-", "").replace("O", "0").replace("I", "1").replace("L", "1"):
        v = _B32.find(ch)
        if v < 0:
            return None  # U is not a symbol
        n = (n << 5) | v
    return n.to_bytes(16, "big")


def kind(text) -> str:
    """'hex', 'base32', 'passphrase', 'short' (a passphrase under 10 characters) or 'empty'."""
    t = (text or "").strip()
    if not t:
        return "empty"
    if _HEX.match(t):
        return "hex"
    if _base32(t) is not None:
        return "base32"
    return "passphrase" if len(unicodedata.normalize("NFC", t)) >= PASSPHRASE_MIN else "short"


def key_bytes(text) -> bytes:
    """The key bytes for a REPL key as typed. ValueError for an empty key or a passphrase under 10 characters."""
    t = (text or "").strip()
    k = kind(t)
    if k == "hex":
        return bytes.fromhex(t)
    if k == "base32":
        return _base32(t)
    if k == "empty":
        raise ValueError("the REPL key is empty")
    if k == "short":
        raise ValueError("a REPL passphrase needs %d characters or more" % PASSPHRASE_MIN)
    return hashlib.pbkdf2_hmac("sha256", unicodedata.normalize("NFC", t).encode(), SALT, ROUNDS, 32)


def new_key_text() -> str:
    """A new random REPL key: 25 base32 symbols in groups of five (125 bits)."""
    n = int.from_bytes(os.urandom(16), "big") & ((1 << 125) - 1)
    s = "".join(_B32[(n >> (5 * i)) & 31] for i in reversed(range(25)))
    return "-".join(s[i:i + 5] for i in range(0, 25, 5))
