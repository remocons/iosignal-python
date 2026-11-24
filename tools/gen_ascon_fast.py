"""Writes src/iosignal/ascon_fast.py: the Ascon permutation in viper on 32-bit halves of each 64-bit lane.

    python3 tools/gen_ascon_fast.py

The rounds are unrolled into straight-line statements (viper has no 64-bit type and calls cost time), so the
file is generated rather than written by hand. ascon.py checks it against the pure Python permutation through
the shared vectors; on a board: the native path must give the same bytes (tests/mpy_vectors.py, and see
agent-work/logs/2026-11-24-ascon-speed-esp32c3.md).
"""
import os

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src", "iosignal", "ascon_fast.py")
ROT = [(19, 28), (61, 39), (1, 6), (10, 17), (7, 41)]  # the rotations of each lane in the linear layer


def ror(v, n, out):
    """Statements setting <out>l/<out>h to the 64-bit lane v rotated right by n."""
    lo, hi = v + "l", v + "h"
    if n < 32:
        return [f"{out}l = ({lo} >> {n}) | ({hi} << {32 - n})", f"{out}h = ({hi} >> {n}) | ({lo} << {32 - n})"]
    m = n - 32
    return [f"{out}l = ({hi} >> {m}) | ({lo} << {32 - m})", f"{out}h = ({lo} >> {m}) | ({hi} << {32 - m})"]


def main():
    lines = []
    w = lines.append
    w('"""Ascon permutation for MicroPython boards: viper (native 32-bit code) on the 40-byte state of ascon.py.')
    w('')
    w('Generated with tools/gen_ascon_fast.py. Each 64-bit lane is two 32-bit halves (little-endian: low word first),')
    w('so no value becomes a big integer. Shipped as source (.py) and compiled on the board, which needs a port with')
    w('the native emitter (ESP32, ESP32-C3, ESP32-S3, RP2040 ...); ascon.py falls back to pure Python without it.')
    w('"""')
    w('import micropython')
    w('')
    w('')
    w('@micropython.viper')
    w('def permute(st, start: int):')
    w('    """Rounds start..11 of the Ascon permutation on st (bytearray of 40, 4-byte aligned)."""')
    w('    p = ptr32(st)')
    for i in range(5):
        w(f'    x{i}l = uint(p[{2 * i}])')
        w(f'    x{i}h = uint(p[{2 * i + 1}])')
    w('    m = uint(0xFFFFFFFF)')
    w('    i = start')
    w('    while i < 12:')
    w('        x2l ^= uint(((15 - i) << 4) | i)')
    for a, b in (("0", "4"), ("4", "3"), ("2", "1")):
        w(f'        x{a}l ^= x{b}l')
        w(f'        x{a}h ^= x{b}h')
    for k in range(5):
        a, b = k, (k + 1) % 5
        w(f'        t{k}l = (x{a}l ^ m) & x{b}l')
        w(f'        t{k}h = (x{a}h ^ m) & x{b}h')
    for a, t in ((0, 1), (1, 2), (2, 3), (3, 4), (4, 0)):
        w(f'        x{a}l ^= t{t}l')
        w(f'        x{a}h ^= t{t}h')
    for a, b in ((1, 0), (0, 4), (3, 2)):
        w(f'        x{a}l ^= x{b}l')
        w(f'        x{a}h ^= x{b}h')
    w('        x2l ^= m')
    w('        x2h ^= m')
    for k, (n1, n2) in enumerate(ROT):
        for s in ror(f"x{k}", n1, "a") + ror(f"x{k}", n2, "b"):
            w('        ' + s)
        w(f'        x{k}l ^= al ^ bl')
        w(f'        x{k}h ^= ah ^ bh')
    w('        i += 1')
    for i in range(5):
        w(f'    p[{2 * i}] = x{i}l')
        w(f'    p[{2 * i + 1}] = x{i}h')
    with open(OUT, "w") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
