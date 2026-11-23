"""X25519 (RFC 7748) in plain Python for MicroPython boards (long ints) and CPython.

    a = os.urandom(32)
    A = x25519.public(a)          # X25519(a, 9)
    shared = x25519.shared(a, B)  # X25519(a, B); ValueError when the result is all zero (a bad peer key)

Montgomery ladder with constant structure (no branches on secret bits; the swaps are arithmetic). Python long
ints are not constant time, so timing is not guaranteed here; the boards use it for one key exchange per REPL
session. Checked against the RFC 7748 vectors (tests/mpy_x25519.py) on both interpreters.
"""

P = 2 ** 255 - 19
A24 = 121665


def _decode_scalar(k):
    k = bytearray(k)
    if len(k) != 32:
        raise ValueError("an X25519 key is 32 bytes")
    k[0] &= 248
    k[31] &= 127
    k[31] |= 64
    return int.from_bytes(bytes(k), "little")


def _decode_u(u):
    if len(u) != 32:
        raise ValueError("an X25519 key is 32 bytes")
    b = bytearray(u)
    b[31] &= 127
    return int.from_bytes(bytes(b), "little") % P


def _ladder(k, u):
    x1 = u
    x2, z2, x3, z3 = 1, 0, u, 1
    swap = 0
    p = P
    for t in range(254, -1, -1):
        kt = (k >> t) & 1
        swap ^= kt
        # conditional swap without a branch on the bit
        mask = -swap  # 0 or -1
        d = mask & (x2 ^ x3)
        x2 ^= d
        x3 ^= d
        d = mask & (z2 ^ z3)
        z2 ^= d
        z3 ^= d
        swap = kt
        a = x2 + z2
        aa = a * a % p
        b = x2 - z2
        bb = b * b % p
        e = aa - bb
        c = x3 + z3
        d = x3 - z3
        da = d * a % p
        cb = c * b % p
        x3 = (da + cb) % p
        x3 = x3 * x3 % p
        z3 = (da - cb) % p
        z3 = x1 * (z3 * z3 % p) % p
        x2 = aa * bb % p
        z2 = e * ((aa + A24 * e) % p) % p
    mask = -swap
    d = mask & (x2 ^ x3)
    x2 ^= d
    d = mask & (z2 ^ z3)
    z2 ^= d
    return x2 * pow(z2, p - 2, p) % p


def x25519(k, u):
    out = _ladder(_decode_scalar(k), _decode_u(u)).to_bytes(32, "little")
    return out


BASE = (9).to_bytes(32, "little")


def public(secret):
    return x25519(secret, BASE)


def shared(secret, peer_public):
    s = x25519(secret, peer_public)
    if s == bytes(32):
        raise ValueError("X25519: low-order peer key")
    return s
