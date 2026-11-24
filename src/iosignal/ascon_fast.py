"""Ascon permutation for MicroPython boards: viper (native 32-bit code) on the 40-byte state of ascon.py.

Generated with tools/gen_ascon_fast.py. Each 64-bit lane is two 32-bit halves (little-endian: low word first),
so no value becomes a big integer. Shipped as source (.py) and compiled on the board, which needs a port with
the native emitter (ESP32, ESP32-C3, ESP32-S3, RP2040 ...); ascon.py falls back to pure Python without it.
"""
import micropython


@micropython.viper
def permute(st, start: int):
    """Rounds start..11 of the Ascon permutation on st (bytearray of 40, 4-byte aligned)."""
    p = ptr32(st)
    x0l = uint(p[0])
    x0h = uint(p[1])
    x1l = uint(p[2])
    x1h = uint(p[3])
    x2l = uint(p[4])
    x2h = uint(p[5])
    x3l = uint(p[6])
    x3h = uint(p[7])
    x4l = uint(p[8])
    x4h = uint(p[9])
    m = uint(0xFFFFFFFF)
    i = start
    while i < 12:
        x2l ^= uint(((15 - i) << 4) | i)
        x0l ^= x4l
        x0h ^= x4h
        x4l ^= x3l
        x4h ^= x3h
        x2l ^= x1l
        x2h ^= x1h
        t0l = (x0l ^ m) & x1l
        t0h = (x0h ^ m) & x1h
        t1l = (x1l ^ m) & x2l
        t1h = (x1h ^ m) & x2h
        t2l = (x2l ^ m) & x3l
        t2h = (x2h ^ m) & x3h
        t3l = (x3l ^ m) & x4l
        t3h = (x3h ^ m) & x4h
        t4l = (x4l ^ m) & x0l
        t4h = (x4h ^ m) & x0h
        x0l ^= t1l
        x0h ^= t1h
        x1l ^= t2l
        x1h ^= t2h
        x2l ^= t3l
        x2h ^= t3h
        x3l ^= t4l
        x3h ^= t4h
        x4l ^= t0l
        x4h ^= t0h
        x1l ^= x0l
        x1h ^= x0h
        x0l ^= x4l
        x0h ^= x4h
        x3l ^= x2l
        x3h ^= x2h
        x2l ^= m
        x2h ^= m
        al = (x0l >> 19) | (x0h << 13)
        ah = (x0h >> 19) | (x0l << 13)
        bl = (x0l >> 28) | (x0h << 4)
        bh = (x0h >> 28) | (x0l << 4)
        x0l ^= al ^ bl
        x0h ^= ah ^ bh
        al = (x1h >> 29) | (x1l << 3)
        ah = (x1l >> 29) | (x1h << 3)
        bl = (x1h >> 7) | (x1l << 25)
        bh = (x1l >> 7) | (x1h << 25)
        x1l ^= al ^ bl
        x1h ^= ah ^ bh
        al = (x2l >> 1) | (x2h << 31)
        ah = (x2h >> 1) | (x2l << 31)
        bl = (x2l >> 6) | (x2h << 26)
        bh = (x2h >> 6) | (x2l << 26)
        x2l ^= al ^ bl
        x2h ^= ah ^ bh
        al = (x3l >> 10) | (x3h << 22)
        ah = (x3h >> 10) | (x3l << 22)
        bl = (x3l >> 17) | (x3h << 15)
        bh = (x3h >> 17) | (x3l << 15)
        x3l ^= al ^ bl
        x3h ^= ah ^ bh
        al = (x4l >> 7) | (x4h << 25)
        ah = (x4h >> 7) | (x4l << 25)
        bl = (x4h >> 9) | (x4l << 23)
        bh = (x4l >> 9) | (x4h << 23)
        x4l ^= al ^ bl
        x4h ^= ah ^ bh
        i += 1
    p[0] = x0l
    p[1] = x0h
    p[2] = x1l
    p[3] = x1h
    p[4] = x2l
    p[5] = x2h
    p[6] = x3l
    p[7] = x3h
    p[8] = x4l
    p[9] = x4h
