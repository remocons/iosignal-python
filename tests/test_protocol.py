# Expected bytes were generated with iosignal src/common/payload.js getSignalPack() (same as the Kotlin PacketsTest).
import unittest

from _path import iosignal  # noqa: F401
from iosignal.protocol import (
    PayloadType, decode_payload, decode_signal, encode_payload, encode_subscription_tags, encode_tag,
    signal_packet, subscription_batches, text_payload,
)

h = bytes.fromhex


class PacketsTest(unittest.TestCase):
    def test_empty(self):
        self.assertEqual("d004726f6f6d00", signal_packet("room", PayloadType.EMPTY).hex())

    def test_text(self):
        self.assertEqual("d004726f6f6d01686900", signal_packet("room", PayloadType.TEXT, text_payload("hi")).hex())

    def test_binary(self):
        self.assertEqual("d004726f6f6d0200ff", signal_packet("room", PayloadType.BINARY, b"\x00\xff").hex())

    def test_object(self):
        self.assertEqual("d004726f6f6d037b2276616c7565223a34327d",
                         signal_packet("room", *encode_payload({"value": 42})).hex())

    def test_mjson(self):
        self.assertEqual("d004726f6f6d045b312c2261225d", signal_packet("room", *encode_payload(1, "a")).hex())

    def test_utf8_tag(self):
        self.assertEqual("d00aebb0a923ec98a8eb8f8401eab09200", signal_packet("방#온도", *encode_payload("값")).hex())

    def test_decode_strips_text_nul(self):
        s = decode_signal(h("d00aebb0a923ec98a8eb8f8401eab09200"))
        self.assertEqual(("방#온도", PayloadType.TEXT, "값"), (s.tag, s.type, s.text))
        self.assertFalse(s.is_direct)

    def test_decode_keeps_binary_bytes(self):
        self.assertEqual("00ff00", decode_signal(h("d004726f6f6d0200ff00")).data.hex())

    def test_decode_rejects_truncated(self):
        self.assertIsNone(decode_signal(h("d00a726f")))

    def test_tag_limit_is_utf8_bytes(self):
        encode_tag("가" * 85)  # 255 bytes
        with self.assertRaises(ValueError):
            encode_tag("가" * 86)
        with self.assertRaises(ValueError):
            encode_tag("", allow_empty=False)

    def test_direct_tags_cannot_be_subscribed(self):
        with self.assertRaises(ValueError):
            encode_subscription_tags("room,@set")
        self.assertEqual(b"room,A@status", encode_subscription_tags("room,A@status"))

    def test_subscription_batches_fit_one_tag_field(self):
        tags = [f"channel-{i:03d}" for i in range(1, 41)]  # 11 bytes each
        batches = subscription_batches(tags)
        self.assertTrue(all(len(b.encode()) <= 255 for b in batches))
        self.assertEqual(tags, [t for b in batches for t in b.split(",")])
        self.assertEqual(2, len(batches))


class TextNulTest(unittest.TestCase):
    def test_args_remove_one_nul_as_js(self):
        s = decode_signal(signal_packet("t", PayloadType.TEXT, text_payload("ab\x00")))
        self.assertEqual(["ab\x00"], s.args)  # JS decodeSignalArgs removes only the sender's NUL
        self.assertEqual(["ab"], decode_signal(signal_packet("t", PayloadType.TEXT, text_payload("ab"))).args)


class CongParserTest(unittest.TestCase):
    def test_large_frame_in_small_chunks(self):
        from iosignal.cong import Parser, pack
        payload = bytes(range(256)) * 4096  # 1 MiB
        wire = pack(b"x") + pack(payload) + pack(b"yz")
        p = Parser(1 << 24)
        out = []
        for i in range(0, len(wire), 1000):
            out += p.feed(wire[i:i + 1000])
        self.assertEqual([b"x", payload, b"yz"], out)
        self.assertEqual((b"", 0), (p.buf, p._need))

    def test_frame_split_in_its_head_and_oversize_skip(self):
        from iosignal.cong import Parser, pack
        p = Parser(10)
        wire = pack(b"a" * 300) + pack(b"ok")  # 300 > max_frame: skipped
        out = []
        for i in range(0, len(wire), 7):
            out += p.feed(wire[i:i + 7])
        self.assertEqual([b"ok"], out)
        self.assertEqual(1, p.oversize)


class PayloadTest(unittest.TestCase):
    def test_types_follow_js_parse_payload(self):
        self.assertEqual((PayloadType.EMPTY, b""), encode_payload())
        self.assertEqual((PayloadType.TEXT, b"42\x00"), encode_payload(42))
        self.assertEqual((PayloadType.TEXT, b"2\x00"), encode_payload(2.0))
        self.assertEqual((PayloadType.BINARY, b"\x01"), encode_payload(bytearray(b"\x01")))
        self.assertEqual((PayloadType.OBJECT, b"null"), encode_payload(None))
        self.assertEqual((PayloadType.OBJECT, b"[1,2]"), encode_payload([1, 2]))
        self.assertEqual(PayloadType.MBA, encode_payload("a", b"\x01")[0])
        with self.assertRaises(TypeError):
            encode_payload(True)

    def test_round_trip(self):
        for values in [(), ("hi",), (b"\x00\xff",), ({"a": [1, None]},), (1, "a", None), ("a", b"\x01", 2, True)]:
            t, data = encode_payload(*values)
            expected = [str(v) if isinstance(v, int) and len(values) == 1 else v for v in values]
            self.assertEqual(expected, decode_payload(t, data), values)

    def test_signal_args(self):
        s = decode_signal(signal_packet("A@x", *encode_payload("a", b"\x01")))
        self.assertEqual(["a", b"\x01"], s.args)
        self.assertEqual(["a", b"\x01"], s.mba().args)

    def test_malformed(self):
        with self.assertRaises(ValueError):
            decode_payload(PayloadType.MJSON, b'{"a":1}')
        with self.assertRaises(ValueError):
            decode_payload(PayloadType.MBA, b"\x01\x02\x03")
        with self.assertRaises(ValueError):
            decode_payload(99, b"")


if __name__ == "__main__":
    unittest.main()
