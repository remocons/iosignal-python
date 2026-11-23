"""CongSocket framing (iosignal src/client/CongFrame.js): IOSignal packets over plain TCP (``cong://``).

Frame: ``[type][length][payload]`` with type 1..4 giving the length size: 1 byte, 2/3/4 bytes little
endian. Runs on CPython 3.10+ and MicroPython 1.28+.
"""

__all__ = ["pack", "Parser", "parse_redirect"]


def pack(payload: bytes) -> bytes:
    n = len(payload)
    if n < 256:
        head = bytes([1, n])
    elif n < 65536:
        head = bytes([2]) + n.to_bytes(2, "little")
    elif n < 1 << 24:
        head = bytes([3]) + n.to_bytes(3, "little")
    else:
        head = bytes([4]) + n.to_bytes(4, "little")
    return head + payload


_HEAD = {1: 2, 2: 3, 3: 4, 4: 5}


class Parser:
    """Feed received bytes with :meth:`feed`; it returns the complete payloads.

    A frame larger than ``max_frame`` is skipped (its bytes are dropped without buffering) and counted in
    ``oversize``. An unknown type byte drops the buffer and counts in ``wrong``, as the JS parser does.
    While a large frame is incomplete, chunks are kept in a list and joined once, not copied per chunk.
    """

    def __init__(self, max_frame: int = 1 << 20):
        self.buf = b""
        self.max_frame = max_frame
        self.skip = 0
        self.oversize = 0
        self.wrong = 0
        self._parts = []  # chunks of an incomplete frame, with self.buf as the first one
        self._have = 0
        self._need = 0  # bytes (head + payload) the incomplete frame needs; 0 when none is pending

    def feed(self, chunk: bytes) -> list:
        if self.skip:
            if len(chunk) <= self.skip:
                self.skip -= len(chunk)
                return []
            chunk = chunk[self.skip:]
            self.skip = 0
        if self._need:
            self._parts.append(bytes(chunk))
            self._have += len(chunk)
            if self._have < self._need:
                return []
            buf = b"".join(self._parts)
            self._parts, self._have, self._need = [], 0, 0
        else:
            buf = self.buf + chunk if self.buf else bytes(chunk)
        frames = []
        while buf:
            head = _HEAD.get(buf[0])
            if head is None:
                self.wrong += 1
                buf = b""
                break
            if len(buf) < head:
                break
            size = int.from_bytes(buf[1:head], "little")
            if size > self.max_frame:
                self.oversize += 1
                available = len(buf) - head
                if available < size:
                    self.skip = size - available
                    buf = b""
                    break
                buf = buf[head + size:]
                continue
            if len(buf) < head + size:
                self._parts, self._have, self._need = [buf], len(buf), head + size
                buf = b""
                break
            frames.append(buf[head:head + size])
            buf = buf[head + size:]
        self.buf = buf
        return frames


def parse_redirect(packet: bytes):
    """A TCP SERVER_REDIRECT ``[0xC5][ipv4 4][port u16 BE]`` as (host, port), or None."""
    if len(packet) != 7:
        return None
    return "%d.%d.%d.%d" % (packet[1], packet[2], packet[3], packet[4]), (packet[5] << 8) | packet[6]
