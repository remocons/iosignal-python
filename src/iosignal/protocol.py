"""Wire constants and signal packets. Values match iosignal ``src/common/constants.js`` and ``payload.js``.

Runs on CPython 3.10+ and MicroPython 1.28+. On MicroPython the constant classes are plain int holders
(no ``enum``), so ``State.READY`` is an int there and has no ``.name``.
"""

import json

try:
    from enum import IntEnum
except ImportError:  # MicroPython
    class IntEnum:  # noqa: D101
        pass

from . import mbp

__all__ = [
    "IOMsg", "PayloadType", "State", "Signal", "TAG_MAX_BYTES",
    "signal_size", "max_message_bytes", "encode_tag", "encode_subscription_tags",
    "encode_payload", "decode_payload", "signal_packet", "text_payload", "tagged",
    "decode_signal", "subscription_batches",
]

TAG_MAX_BYTES = 255


class IOMsg(IntEnum):
    """One-byte message types (iosignal IOMsg and boho BohoMsg)."""

    # Boho reserves 0xB0~0xBF.
    SERVER_TIME_NONCE = 0xB0  # SERVER_READY
    AUTH_REQ = 0xB1
    AUTH_RES = 0xB2
    AUTH_FAIL = 0xB3
    ENC_E2E = 0xB7
    ENC_488 = 0xB8

    SERVER_READY = 0xC0
    CID_REQ = 0xC1
    CID_RES = 0xC2
    QUOTA_LEVEL = 0xC3  # servers before iosignal 9.0
    AUTH_CLEAR = 0xC4
    SERVER_REDIRECT = 0xC5
    QUOTA = 0xC6  # iosignal 9.0+: u32 LE maxMessageBytes (0: unlimited)
    LOOP = 0xCB
    ECHO = 0xCC
    PING = 0xCD
    PONG = 0xCE
    CLOSE = 0xCF

    SIGNAL = 0xD0
    SIGNAL_REQ = 0xD1
    SIGNAL_E2E = 0xD2
    SUBSCRIBE = 0xD3
    SUBSCRIBE_REQ = 0xD4
    UNSUBSCRIBE = 0xD5
    SERVER_SIGNAL = 0xD6
    IAM = 0xD9
    IAM_RES = 0xDA
    SET = 0xDB
    RESPONSE_MBP = 0xDD
    CALL = 0xDE

    OVER_SIZE = 0xFE


class PayloadType(IntEnum):
    EMPTY = 0
    TEXT = 1
    BINARY = 2
    OBJECT = 3  # one JSON value (object, array or null)
    MJSON = 4  # a top-level JSON array of arguments
    MBA = 5  # meta-buffer-pack arguments


class State(IntEnum):
    """Client connection state (iosignal STATE)."""

    CONNECTING = 0
    OPEN = 1
    CLOSING = 2
    CLOSED = 3
    STOP = 4
    REDIRECTING = 5
    SERVER_READY = 10
    AUTH_REQ = 11  # sent the login proof, waiting for AUTH_RES
    AUTH_RES = 12  # login accepted, CID request follows
    AUTH_FAIL = 13  # wrong ID or key; the socket stays open without a CID
    AUTH_CLEAR = 14  # the server cleared the login; the client stops
    READY = 19


class Signal:
    """A received signal.

    Direct (unicast) signals arrive with the sender CID removed, e.g. ``@set``.
    CID-subscription signals keep the publisher CID, e.g. ``A@status``.
    """

    __slots__ = ("tag", "type", "data", "e2e")

    def __init__(self, tag: str, type: int, data: bytes, e2e: bool = False):  # noqa: A002
        self.tag = tag
        self.type = type
        self.data = data
        self.e2e = e2e  # SIGNAL_E2E: data is still sealed with the sender's E2E key (IOSignal.decrypt_e2e)

    def __repr__(self) -> str:
        return "Signal(tag=%r, type=%r, data=%r)" % (self.tag, self.type, self.data)

    def __eq__(self, other) -> bool:
        return (isinstance(other, Signal) and self.tag == other.tag and self.type == other.type
                and self.data == other.data)

    def __hash__(self) -> int:
        return hash((self.tag, self.type, self.data))

    @property
    def is_direct(self) -> bool:
        return self.tag.startswith("@")

    @property
    def text(self) -> str:
        """TEXT payload, or the JSON text of an OBJECT/MJSON payload."""
        return self.data.decode()

    @property
    def args(self) -> list:
        """Arguments as JS handlers receive them (see :func:`decode_payload`)."""
        if self.type == PayloadType.TEXT:
            # data already lost its trailing NUL in decode_signal; JS removes only one.
            return [self.data.decode()]
        return decode_payload(self.type, self.data)

    def mba(self):
        return mbp.unpack(self.data) if self.type == PayloadType.MBA else None


def signal_size(level: int) -> int:
    """Largest packet per quota level (iosignal quotaTable.js signalSize)."""
    if level == 1:
        return 255
    if level in (2, 11):
        return 65_535
    if level in (3, 12):
        return 1_048_576
    if level in (200, 255):
        return 20 * 1_048_576
    return 1500  # 0, 10 and unknown levels


def max_message_bytes(packet: bytes) -> int | None:
    """maxMessageBytes of a QUOTA packet (u32 LE after the type). None means unlimited (0 on the wire)."""
    n = int.from_bytes(packet[1:5], "little")
    return n or None


def encode_tag(tag: str, allow_empty: bool = True) -> bytes:
    """Wire tags carry a one-byte length, so the limit is 255 UTF-8 bytes."""
    encoded = tag.encode()
    if (not allow_empty and not encoded) or len(encoded) > TAG_MAX_BYTES:
        raise ValueError(f"tag UTF-8 byte length must be {0 if allow_empty else 1}~255")
    return encoded


def encode_subscription_tags(tags: str) -> bytes:
    """Direct-receive tags (``@...``) are local handler names, never server subscriptions."""
    encoded = encode_tag(tags)
    if any(part.startswith("@") for part in tags.split(",")):
        raise ValueError("direct signal tags need no subscription; use listen() instead")
    return encoded


def text_payload(value: str) -> bytes:
    """TEXT payloads carry a trailing NUL for C/C++ receivers."""
    return value.encode() + b"\x00"


def encode_payload(*values) -> tuple:
    """Payload type and bytes for ``signal(tag, *values)``, as JS ``parsePayload`` chooses them.

    none: EMPTY; one str/int/float: TEXT; one bytes-like: BINARY; one dict/list/tuple/None: OBJECT;
    two or more: MBA if any is bytes-like, else MJSON.
    """
    if not values:
        return PayloadType.EMPTY, b""
    if len(values) == 1:
        v = values[0]
        if isinstance(v, str):
            return PayloadType.TEXT, text_payload(v)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return PayloadType.TEXT, text_payload(mbp.number_text(v))
        if isinstance(v, (bytes, bytearray, memoryview)):
            return PayloadType.BINARY, bytes(v)
        if v is None or isinstance(v, (dict, list, tuple)):
            return PayloadType.OBJECT, mbp.to_json(v).encode()
        raise TypeError(f"unknown payload argument {type(v).__name__}")
    if any(isinstance(v, (bytes, bytearray, memoryview)) for v in values):
        return PayloadType.MBA, mbp.pack(mbp.args(values))
    return PayloadType.MJSON, mbp.to_json(list(values)).encode()


def decode_payload(payload_type: int, data: bytes) -> list:
    """Arguments of a payload as JS ``decodeSignalArgs`` gives them.

    EMPTY [], TEXT [str] (trailing NUL removed), BINARY [bytes], OBJECT [value], MJSON [*values],
    MBA [*args]. Raises ValueError for malformed JSON/MBA or an unknown type.
    """
    if payload_type == PayloadType.EMPTY:
        return []
    if payload_type == PayloadType.TEXT:
        return [(data[:-1] if data[-1:] == b"\x00" else data).decode()]
    if payload_type == PayloadType.BINARY:
        return [bytes(data)]
    if payload_type == PayloadType.OBJECT:
        return [json.loads(data.decode())]
    if payload_type == PayloadType.MJSON:
        values = json.loads(data.decode())
        if not isinstance(values, list):
            raise ValueError("MJSON payload is not an array")
        return values
    if payload_type == PayloadType.MBA:
        unpacked = mbp.unpack(data)
        if unpacked is None:
            raise ValueError("malformed MBA payload")
        return unpacked.args
    raise ValueError(f"unknown payload type {payload_type}")


def signal_packet(tag: str, payload_type: int, payload: bytes = b"") -> bytes:
    """``[SIGNAL, tagLen, tag, payloadType, payload]``."""
    t = encode_tag(tag)
    return bytes([IOMsg.SIGNAL, len(t)]) + t + bytes([payload_type]) + payload


def tagged(msg_type: int, encoded_tag: bytes) -> bytes:
    """``[msgType, tagLen, tag]`` for SUBSCRIBE, UNSUBSCRIBE and similar."""
    return bytes([msg_type, len(encoded_tag)]) + encoded_tag


def decode_signal(packet: bytes) -> Signal | None:
    """Decodes a SIGNAL/SIGNAL_E2E packet. TEXT loses its trailing NUL. None if truncated."""
    if len(packet) < 3:
        return None
    tag_len = packet[1]
    if len(packet) < 3 + tag_len:
        return None
    tag = packet[2:2 + tag_len].decode()
    payload_type = packet[2 + tag_len]
    data = bytes(packet[3 + tag_len:])
    if payload_type == PayloadType.TEXT and data[-1:] == b"\x00":
        data = data[:-1]
    return Signal(tag, payload_type, data)


def subscription_batches(tags) -> list:
    """Splits remembered subscriptions into comma-joined batches that each fit one tag field."""
    batches: list[str] = []
    batch: list[str] = []
    size_sum = 0
    for tag in tags:
        size = len(tag.encode())
        if batch and size_sum + 1 + size > TAG_MAX_BYTES:
            batches.append(",".join(batch))
            batch, size_sum = [], 0
        size_sum += (1 if batch else 0) + size
        batch.append(tag)
    if batch:
        batches.append(",".join(batch))
    return batches
