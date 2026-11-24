"""Ascon (NIST SP 800-232): Ascon-AEAD128 and Ascon-CXOF128, as Boho uses them.

The same algorithm as the JS boho package (src/ascon.js), the Arduino BohoAscon.cpp and the Kotlin
client. Runs on CPython 3.10+ and MicroPython 1.28+ (plain ints, ``int.from_bytes``, no typing).
The state is a 40-byte bytearray (five 64-bit words, little-endian). The permutation runs as native code on
MicroPython boards that have the native emitter (``ascon_fast``, viper on 32-bit halves: no big integers), and as
the pure Python ``_permute`` on five ints elsewhere (CPython, ports without viper).
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
    M = _M
    for c in _RC[12 - rounds:]:
        x2 ^= c
        x0 ^= x4
        x4 ^= x3
        x2 ^= x1
        t0 = (x0 ^ M) & x1
        t1 = (x1 ^ M) & x2
        t2 = (x2 ^ M) & x3
        t3 = (x3 ^ M) & x4
        t4 = (x4 ^ M) & x0
        x0 ^= t1
        x1 ^= t2
        x2 ^= t3
        x3 ^= t4
        x4 ^= t0
        x1 ^= x0
        x0 ^= x4
        x3 ^= x2
        x2 ^= M
        x0 ^= ((x0 >> 19) | (x0 << 45)) & M ^ ((x0 >> 28) | (x0 << 36)) & M
        x1 ^= ((x1 >> 61) | (x1 << 3)) & M ^ ((x1 >> 39) | (x1 << 25)) & M
        x2 ^= ((x2 >> 1) | (x2 << 63)) & M ^ ((x2 >> 6) | (x2 << 58)) & M
        x3 ^= ((x3 >> 10) | (x3 << 54)) & M ^ ((x3 >> 17) | (x3 << 47)) & M
        x4 ^= ((x4 >> 7) | (x4 << 57)) & M ^ ((x4 >> 41) | (x4 << 23)) & M
    s[0], s[1], s[2], s[3], s[4] = x0, x1, x2, x3, x4


def _permute_bytes(st, rounds):
    s = [int.from_bytes(st[i:i + 8], "little") for i in (0, 8, 16, 24, 32)]
    _permute(s, rounds)
    for k in range(5):
        st[8 * k:8 * k + 8] = s[k].to_bytes(8, "little")


# Known answers for the self-check below: the 12- and 8-round permutation of bytes 0..39 (pure Python result,
# which the shared vectors check).
_KAT12 = "797b2ecb0434b0947f236705e8a5225a3ed84b502f5afbc8864315d58ac016dc761b7a8367ad8288"
_KAT8 = "5e07caf80e7ce0c6703a053dac2a9d562d914f4bdda4df4e954ec9a22d197efd1b8d85aec2754324"


def _native_ok(fn):
    """True when the native permutation gives the known answers (a CPU or MicroPython version whose viper code
    compiles but computes something else, e.g. big-endian, an emitter bug or changed type rules, is not used)."""
    from binascii import hexlify
    for start, want in ((0, _KAT12), (4, _KAT8)):
        st = bytearray(range(40))
        fn(st, start)
        if hexlify(st).decode() != want:
            return False
    return True


try:
    from .ascon_fast import permute as _native

    if not _native_ok(_native):
        raise ValueError("ascon_fast gives wrong results here")

    def _perm(st, rounds):
        _native(st, 12 - rounds)

    NATIVE = True
except Exception:  # noqa: BLE001 - CPython, a port without the native emitter, or a failed self-check
    _perm = _permute_bytes
    NATIVE = False

_AEAD_IV_B = _AEAD_IV.to_bytes(8, "little")
_CXOF_IV_B = _CXOF_IV.to_bytes(8, "little")


def _xor(st, off, data, start, n):
    for k in range(n):
        st[off + k] ^= data[start + k]


def _start(key, nonce, ad):
    if len(key) != 16 or len(nonce) != 16:
        raise ValueError("key and nonce must be 16 bytes")
    key = bytes(key)
    st = bytearray(_AEAD_IV_B + key + bytes(nonce))
    _perm(st, 12)
    _xor(st, 24, key, 0, 16)
    if ad:
        i = 0
        while len(ad) - i >= 16:
            _xor(st, 0, ad, i, 16)
            _perm(st, 8)
            i += 16
        rest = len(ad) - i
        _xor(st, 0, ad, i, rest)
        st[rest] ^= 0x01
        _perm(st, 8)
    st[39] ^= 0x80
    return st, key


def _finish(st, key):
    _xor(st, 16, key, 0, 16)
    _perm(st, 12)
    _xor(st, 24, key, 0, 16)
    return bytes(st[24:40])


def encrypt(key, nonce, plain, ad=b"", tag_length=16):
    """Ascon-AEAD128: (ciphertext, the first ``tag_length`` bytes of the tag)."""
    plain = bytes(plain)
    st, key = _start(key, nonce, bytes(ad))
    out = bytearray()
    i = 0
    while len(plain) - i >= 16:
        _xor(st, 0, plain, i, 16)
        out += st[0:16]
        _perm(st, 8)
        i += 16
    rest = len(plain) - i
    _xor(st, 0, plain, i, rest)
    st[rest] ^= 0x01
    out += st[0:rest]
    tag = _finish(st, key)
    return bytes(out), tag[:tag_length]


def decrypt(key, nonce, cipher, ad, tag):
    """Ascon-AEAD128 decryption; None when ``tag`` (4..16 bytes) does not match."""
    if not 4 <= len(tag) <= 16:
        return None
    cipher = bytes(cipher)
    st, key = _start(key, nonce, bytes(ad))
    out = bytearray(len(cipher))
    i = 0
    n = len(cipher)
    while i < n:
        take = 16 if n - i >= 16 else n - i
        for k in range(take):
            c = cipher[i + k]
            out[i + k] = st[k] ^ c
            st[k] = c
        if take == 16:
            _perm(st, 8)
            i += 16
        else:
            i += take
            break
    rest = n % 16
    st[rest] ^= 0x01
    full = _finish(st, key)
    diff = 0
    for a, b in zip(full[:len(tag)], tag):
        diff |= a ^ b
    if diff:
        return None
    return bytes(out)


def _absorb8(st, data):
    i = 0
    while len(data) - i >= 8:
        _xor(st, 0, data, i, 8)
        _perm(st, 12)
        i += 8
    rest = len(data) - i
    _xor(st, 0, data, i, rest)
    st[rest] ^= 0x01
    _perm(st, 12)


def cxof128(data, out_length, custom=b""):
    """Ascon-CXOF128: ``out_length`` bytes, with a customization string of at most 256 bytes."""
    custom = bytes(custom)
    if len(custom) > 256:
        raise ValueError("custom must be at most 256 bytes")
    st = bytearray(_CXOF_IV_B + bytes(32))
    _perm(st, 12)
    _xor(st, 0, (len(custom) * 8).to_bytes(8, "little"), 0, 8)
    _perm(st, 12)
    _absorb8(st, custom)
    _absorb8(st, bytes(data))
    out = bytearray()
    while len(out) < out_length:
        if out:
            _perm(st, 12)
        out += st[0:8]
    return bytes(out[:out_length])
