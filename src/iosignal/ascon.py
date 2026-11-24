"""Ascon (NIST SP 800-232): Ascon-AEAD128 and Ascon-CXOF128, as Boho uses them.

The same algorithm as the JS boho package (src/ascon.js), the Arduino BohoAscon.cpp and the Kotlin
client. Runs on CPython 3.10+ and MicroPython 1.28+ (plain ints, ``int.from_bytes``, no typing).
The state is five 64-bit words; bytes are loaded little-endian.
"""

__all__ = ["encrypt", "decrypt", "cxof128"]

_M = 0xFFFFFFFFFFFFFFFF
_AEAD_IV = 0x00001000808C0001
_CXOF_IV = 0x0000080000CC0004
_TOP = 1 << 63
# Round constants of the last 12 rounds (P12 uses all, P8 the last 8).
_RC = [((0x0F - i) << 4) | i for i in range(12)]


def _le(b):
    return int.from_bytes(b, "little")


def _ror(x, n):
    return ((x >> n) | (x << (64 - n))) & _M


def _permute(s, rounds):
    x0, x1, x2, x3, x4 = s
    for i in range(12 - rounds, 12):
        x2 ^= _RC[i]
        x0 ^= x4
        x4 ^= x3
        x2 ^= x1
        t0 = (x0 ^ _M) & x1
        t1 = (x1 ^ _M) & x2
        t2 = (x2 ^ _M) & x3
        t3 = (x3 ^ _M) & x4
        t4 = (x4 ^ _M) & x0
        x0 ^= t1
        x1 ^= t2
        x2 ^= t3
        x3 ^= t4
        x4 ^= t0
        x1 ^= x0
        x0 ^= x4
        x3 ^= x2
        x2 ^= _M
        x0 ^= _ror(x0, 19) ^ _ror(x0, 28)
        x1 ^= _ror(x1, 61) ^ _ror(x1, 39)
        x2 ^= _ror(x2, 1) ^ _ror(x2, 6)
        x3 ^= _ror(x3, 10) ^ _ror(x3, 17)
        x4 ^= _ror(x4, 7) ^ _ror(x4, 41)
    s[0], s[1], s[2], s[3], s[4] = x0, x1, x2, x3, x4


def _pad16(b):
    return bytes(b) + b"\x01" + bytes(15 - len(b))


def _start(key, nonce, ad):
    if len(key) != 16 or len(nonce) != 16:
        raise ValueError("key and nonce must be 16 bytes")
    k0, k1 = _le(key[:8]), _le(key[8:])
    s = [_AEAD_IV, k0, k1, _le(nonce[:8]), _le(nonce[8:])]
    _permute(s, 12)
    s[3] ^= k0
    s[4] ^= k1
    if ad:
        i = 0
        while len(ad) - i >= 16:
            s[0] ^= _le(ad[i:i + 8])
            s[1] ^= _le(ad[i + 8:i + 16])
            _permute(s, 8)
            i += 16
        last = _pad16(ad[i:])
        s[0] ^= _le(last[:8])
        s[1] ^= _le(last[8:])
        _permute(s, 8)
    s[4] ^= _TOP
    return s, k0, k1


def _finish(s, k0, k1):
    s[2] ^= k0
    s[3] ^= k1
    _permute(s, 12)
    return (s[3] ^ k0).to_bytes(8, "little") + (s[4] ^ k1).to_bytes(8, "little")


def _rate(s):
    return s[0].to_bytes(8, "little") + s[1].to_bytes(8, "little")


def encrypt(key, nonce, plain, ad=b"", tag_length=16):
    """Ascon-AEAD128: (ciphertext, the first ``tag_length`` bytes of the tag)."""
    plain = bytes(plain)
    s, k0, k1 = _start(key, nonce, bytes(ad))
    out = bytearray()
    i = 0
    while len(plain) - i >= 16:
        s[0] ^= _le(plain[i:i + 8])
        s[1] ^= _le(plain[i + 8:i + 16])
        out += _rate(s)
        _permute(s, 8)
        i += 16
    rest = len(plain) - i
    last = _pad16(plain[i:])
    s[0] ^= _le(last[:8])
    s[1] ^= _le(last[8:])
    out += _rate(s)[:rest]
    tag = _finish(s, k0, k1)
    return bytes(out), tag[:tag_length]


def decrypt(key, nonce, cipher, ad, tag):
    """Ascon-AEAD128 decryption; None when ``tag`` (4..16 bytes) does not match."""
    if not 4 <= len(tag) <= 16:
        return None
    cipher = bytes(cipher)
    s, k0, k1 = _start(key, nonce, bytes(ad))
    out = bytearray()
    i = 0
    while len(cipher) - i >= 16:
        c0, c1 = _le(cipher[i:i + 8]), _le(cipher[i + 8:i + 16])
        out += (s[0] ^ c0).to_bytes(8, "little") + (s[1] ^ c1).to_bytes(8, "little")
        s[0], s[1] = c0, c1
        _permute(s, 8)
        i += 16
    rest = len(cipher) - i
    r = bytearray(_rate(s))
    for k in range(rest):
        c = cipher[i + k]
        out.append(r[k] ^ c)
        r[k] = c
    r[rest] ^= 0x01
    s[0], s[1] = _le(r[:8]), _le(r[8:])
    full = _finish(s, k0, k1)
    diff = 0
    for a, b in zip(full[:len(tag)], tag):
        diff |= a ^ b
    if diff:
        return None
    return bytes(out)


def _absorb8(s, data):
    i = 0
    while len(data) - i >= 8:
        s[0] ^= _le(data[i:i + 8])
        _permute(s, 12)
        i += 8
    last = bytes(data[i:]) + b"\x01" + bytes(7 - (len(data) - i))
    s[0] ^= _le(last)
    _permute(s, 12)


def cxof128(data, out_length, custom=b""):
    """Ascon-CXOF128: ``out_length`` bytes, with a customization string of at most 256 bytes."""
    custom = bytes(custom)
    if len(custom) > 256:
        raise ValueError("custom must be at most 256 bytes")
    s = [_CXOF_IV, 0, 0, 0, 0]
    _permute(s, 12)
    s[0] ^= len(custom) * 8
    _permute(s, 12)
    _absorb8(s, custom)
    _absorb8(s, bytes(data))
    out = bytearray()
    while len(out) < out_length:
        if out:
            _permute(s, 12)
        out += s[0].to_bytes(8, "little")
    return bytes(out[:out_length])
