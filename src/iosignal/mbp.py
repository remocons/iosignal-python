"""meta-buffer-pack 2.x, as iosignal uses it for MBA signals, RPC calls and responses.

Layout: ``data... | metaJSON | metaLength (uint16 BE)``, where metaJSON is
``[[name, type, offset, size], ...]``. Fields whose name contains ``#`` are data only and stay out
of the meta; a pack without meta fields is the data alone.

Matches the JS package ``meta-buffer-pack`` (``MB``, ``MBA``, ``pack``, ``unpack``).

Runs on CPython 3.10+ and MicroPython 1.28+ (no dataclasses, typing, decimal or __future__ here;
annotations are evaluated by CPython and ignored by MicroPython).
"""

import json
import math
import struct

__all__ = [
    "Field", "Unpacked", "typed", "u8", "u16", "string", "binary", "value", "args",
    "pack", "unpack", "number_text", "to_json",
]

_BYTES = (bytes, bytearray, memoryview)


class Field:
    """One field. A ``numeric`` name is written as a JSON number, as MBA argument indexes are."""

    __slots__ = ("name", "type", "data", "numeric")

    def __init__(self, name: str, type: str, data: bytes, numeric: bool = False):  # noqa: A002
        self.name = name
        self.type = type
        self.data = data
        self.numeric = numeric

    def __repr__(self) -> str:
        return "Field(%r, %r, %r, numeric=%r)" % (self.name, self.type, self.data, self.numeric)

    def __eq__(self, other) -> bool:
        return (isinstance(other, Field) and self.name == other.name and self.type == other.type
                and self.data == other.data and self.numeric == other.numeric)


class Unpacked:
    """Fields by name (MBA indexes as ``"0"``, ``"1"``, ...) and the MBA arguments in order."""

    def __init__(self, fields: dict, args: list = None):
        self.fields = fields
        self.args = [] if args is None else args

    def __getitem__(self, name: str):
        return self.fields[name]

    def __contains__(self, name: str) -> bool:
        return name in self.fields

    def get(self, name: str, default=None):
        return self.fields.get(name, default)

    def __repr__(self) -> str:
        return "Unpacked(%r, %r)" % (self.fields, self.args)

    def __eq__(self, other) -> bool:
        return isinstance(other, Unpacked) and self.fields == other.fields and self.args == other.args


def _digits_of(text: str):
    """Shortest round-trip digits of a positive float repr, as (digits, exponent): value = 0.digits... no:
    value = int(digits) * 10 ** exponent. ``repr`` gives the same digits on CPython and MicroPython."""
    mantissa, exp = text, 0
    for mark in ("e", "E"):
        if mark in text:
            mantissa, e = text.split(mark)
            exp = int(e)
    if "." in mantissa:
        whole, frac = mantissa.split(".")
    else:
        whole, frac = mantissa, ""
    digits = (whole + frac).lstrip("0")
    exp -= len(frac)
    stripped = digits.rstrip("0")
    exp += len(digits) - len(stripped)
    return stripped, exp


def number_text(n) -> str:
    """JS ``String(number)``: integral floats have no ``.0``."""
    if isinstance(n, bool):
        raise TypeError("bool is not a number here")
    if isinstance(n, int):
        return str(n)
    if math.isnan(n):
        return "NaN"
    if math.isinf(n):
        return "Infinity" if n > 0 else "-Infinity"
    if n == 0:
        return "0"
    # ECMAScript Number::toString from the shortest round-trip digits.
    sign = "-" if n < 0 else ""
    digits, exp = _digits_of(repr(abs(n)))
    k = len(digits)
    point = exp + k  # decimal point position relative to the digits
    if k <= point <= 21:
        return sign + digits + "0" * (point - k)
    if 0 < point <= 21:
        return sign + digits[:point] + "." + digits[point:]
    if -6 < point <= 0:
        return sign + "0." + "0" * -point + digits
    e = point - 1
    mantissa = digits if k == 1 else digits[0] + "." + digits[1:]
    return sign + mantissa + "e" + ("+" if e >= 0 else "-") + str(abs(e))


def to_json(v) -> str:
    """JS ``JSON.stringify`` for plain values: compact, non-ASCII kept (MicroPython keeps it anyway)."""
    try:
        return json.dumps(v, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except TypeError:  # MicroPython: no ensure_ascii/allow_nan keywords
        return json.dumps(v, separators=(",", ":"))


def _parse_type(type_: str):
    """struct format (without byte order) and width of a typed number; ('', 0) for B/S/N/O; raises for unknown."""
    t = type_.upper()
    if "8" in t:
        return ("b" if "I" in t else "B"), 1
    if "16" in t:
        return ("h" if "I" in t else "H"), 2
    if "32" in t:
        return ("i" if "I" in t else "I"), 4
    if "F" in t:
        return "f", 4
    if t == "!":
        return "?", 1
    if t in ("B", "S", "N", "O"):
        return "", 0
    raise ValueError("invalid MBP type %r" % (type_,))


def typed(name: str, type_: str, number) -> Field:
    """JS ``MB(name, type, number)``: '8', 'I8', '16', '16L', 'I16L', '32', 'I32', 'F', 'FL', or 'N' (decimal text)."""
    t = type_.upper()
    if "N" in t and not any(k in t for k in ("8", "16", "32", "F")):
        return Field(name, t, number_text(number).encode())
    fmt, _ = _parse_type(t)
    if fmt in ("", "?"):
        raise ValueError("not a number type: %r" % (type_,))
    order = "<" if "L" in t else ">"
    return Field(name, t, struct.pack(order + fmt, number))


def u8(name: str, number: int) -> Field:
    return typed(name, "8", number)


def u16(name: str, number: int) -> Field:
    return typed(name, "16", number)


def string(name: str, text: str) -> Field:
    return Field(name, "S", text.encode())


def binary(name: str, data) -> Field:
    return Field(name, "B", bytes(data))


def value(name: str, v, numeric: bool = False) -> Field:
    """A value typed as JS ``MB(name, value)`` would type it.

    str ``S``, bytes-like ``B``, bool ``!``, int/float ``N`` (decimal text), dict/list/tuple/None ``O`` (JSON).
    """
    if isinstance(v, str):
        return Field(name, "S", v.encode(), numeric)
    if isinstance(v, _BYTES):
        return Field(name, "B", bytes(v), numeric)
    if isinstance(v, bool):
        return Field(name, "!", b"\x01" if v else b"\x00", numeric)
    if isinstance(v, (int, float)):
        return Field(name, "N", number_text(v).encode(), numeric)
    if v is None or isinstance(v, (dict, list, tuple)):
        return Field(name, "O", to_json(v).encode(), numeric)
    raise TypeError("unsupported MBP value %s" % (type(v).__name__,))


def args(values) -> list:
    """JS ``MBA(...values)``: arguments named by index."""
    return [value(str(i), v, True) for i, v in enumerate(values)]


def pack(*fields) -> bytes:
    """Packs fields in order. Lists of fields (from :func:`args`) are flattened in place."""
    flat = []
    for f in fields:
        if isinstance(f, Field):
            flat.append(f)
        else:
            flat.extend(f)
    out = bytearray()
    meta = []
    for f in flat:
        if "#" not in f.name:
            meta.append([int(f.name) if f.numeric else f.name, f.type, len(out), len(f.data)])
        out += f.data
    if meta:
        meta_bytes = to_json(meta).encode()
        if len(meta_bytes) > 0xFFFF:
            raise ValueError("MBP meta is longer than 65535 bytes")
        out += meta_bytes
        out += len(meta_bytes).to_bytes(2, "big")
    return bytes(out)


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _read(type_: str, b: bytes, offset: int, length: int):
    t = type_.upper()
    fmt, width = _parse_type(t)
    chunk = b[offset:offset + length]
    if width:
        if fmt == "?":
            return chunk[0] == 1
        return struct.unpack(("<" if "L" in t else ">") + fmt, chunk)[0]
    if t == "B":
        return bytes(chunk)
    text = chunk.decode()
    if t == "S":
        return text
    if t == "N":
        s = text.strip()
        if s == "":
            return 0  # JS Number('') is 0
        try:
            return int(s)
        except ValueError:
            pass
        try:
            return float(s)  # also 'NaN', 'Infinity'
        except ValueError:
            return math.nan
    return json.loads(text)  # 'O'


def unpack(buffer):
    """Reads a pack with trailing meta. None for a pack without meta or with invalid meta, as JS returns undefined."""
    b = bytes(buffer)
    if len(b) <= 2:
        return None
    meta_len = int.from_bytes(b[-2:], "big")
    data_size = len(b) - 2 - meta_len
    if meta_len == 0 or data_size < 0:
        return None
    try:
        meta = json.loads(b[data_size:data_size + meta_len].decode())
    except (UnicodeError, ValueError):
        return None
    if not isinstance(meta, list) or not meta:
        return None
    fields = {}
    for entry in meta:
        if not isinstance(entry, list) or not 3 <= len(entry) <= 5:
            return None
        name, type_, offset = entry[0], entry[1], entry[2]
        if isinstance(name, str):
            pass
        elif _is_int(name) and name >= 0:
            name = str(name)
        else:
            return None
        if not isinstance(type_, str):
            return None
        try:
            _, width = _parse_type(type_)
        except ValueError:
            return None
        if len(entry) > 3:
            length = entry[3]
            if not _is_int(length) or length < 0 or (width and length != width):
                return None
        elif width:
            length = width
        else:
            return None
        if not _is_int(offset) or offset < 0 or offset > data_size or length > data_size - offset:
            return None
        try:
            fields[name] = _read(type_, b, offset, length)
        except (UnicodeError, ValueError, struct.error if hasattr(struct, "error") else ValueError):
            return None
    arguments = []
    while str(len(arguments)) in fields:
        arguments.append(fields[str(len(arguments))])
    return Unpacked(fields, arguments)
