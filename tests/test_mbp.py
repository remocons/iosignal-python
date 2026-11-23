# Expected bytes come from meta-buffer-pack 2.1.4 and iosignal getSignalPack (same vectors as the Kotlin MbpTest).
import math
import unittest

from _path import iosignal  # noqa: F401
from iosignal import mbp
from iosignal.protocol import IOMsg, PayloadType, signal_packet, signal_size, max_message_bytes

h = bytes.fromhex

JS_MBA = (
    "613432332e35010102037b2278223a317d5b5b302c2253222c302c315d2c5b312c224e222c312c325d2c5b322c224e222c332c335d"
    "2c5b332c2221222c362c315d2c5b342c2242222c372c335d2c5b352c224f222c31302c375d5d004a"
)


class MbpTest(unittest.TestCase):
    def test_mba_matches_js(self):
        packed = mbp.pack(mbp.args(["a", 42, 3.5, True, b"\x01\x02\x03", {"x": 1}]))
        self.assertEqual(JS_MBA, packed.hex())

    def test_mba_unpacks_js_values(self):
        u = mbp.unpack(h(JS_MBA))
        self.assertEqual(["a", 42, 3.5, True, b"\x01\x02\x03", {"x": 1}], u.args)
        self.assertIs(True, u.args[3])

    def test_mba_signal_matches_js(self):
        self.assertEqual(
            "d0066b74236d6261056101025b5b302c2253222c302c315d2c5b312c2242222c312c325d5d0019",
            signal_packet("kt#mba", PayloadType.MBA, mbp.pack(mbp.args(["a", b"\x01\x02"]))).hex(),
        )

    def test_call_matches_js(self):
        fields = [mbp.u8("#MsgType", IOMsg.CALL), mbp.u16("mid", 7), mbp.string("target", "svc"), mbp.string("topic", "echo")]
        self.assertEqual(
            "de00077376636563686f6869335b5b226d6964222c223136222c312c325d2c5b22746172676574222c2253222c332c335d2c5b2274"
            "6f706963222c2253222c362c345d2c5b302c2253222c31302c325d2c5b312c224e222c31322c315d5d0051",
            mbp.pack(fields, mbp.args(["hi", 3])).hex(),
        )
        self.assertEqual(
            "de010273766370696e675b5b226d6964222c223136222c312c325d2c5b22746172676574222c2253222c332c335d2c5b22746f7069"
            "63222c2253222c362c345d5d0037",
            mbp.pack(mbp.u8("#MsgType", IOMsg.CALL), mbp.u16("mid", 258), mbp.string("target", "svc"),
                     mbp.string("topic", "ping")).hex(),
        )

    def test_responses_from_js_server(self):
        ok = mbp.unpack(h(
            "dd0000077b226f6b223a747275652c226e223a5b312c325d7d5b5b22737461747573222c2238222c312c315d2c5b226d6964222c2231"
            "36222c322c325d2c5b22626f6479222c224f222c342c32315d5d0037"
        ))
        self.assertEqual(0, ok["status"])
        self.assertEqual(7, ok["mid"])
        self.assertEqual({"ok": True, "n": [1, 2]}, ok["body"])
        self.assertEqual([], ok.args)

        fail = mbp.unpack(h(
            "ddff0009554e4b4e4f574e5f434f4d4d414e445b5b22737461747573222c2238222c312c315d2c5b226d6964222c223136222c322c32"
            "5d2c5b22626f6479222c2253222c342c31355d5d0037"
        ))
        self.assertEqual(255, fail["status"])
        self.assertEqual(9, fail["mid"])
        self.assertEqual("UNKNOWN_COMMAND", fail["body"])

    def test_typed_numbers_from_js(self):
        u = mbp.unpack(h(
            "01fe0102fffffffbee6b28003fc000005b5b226938222c224938222c312c315d2c5b227531366c222c2231364c222c322c325d2c5b22"
            "693332222c22493332222c342c345d2c5b22753332222c223332222c382c345d2c5b2266222c2246222c31322c345d5d0056"
        ))
        self.assertEqual(-2, u["i8"])
        self.assertEqual(513, u["u16l"])
        self.assertEqual(-5, u["i32"])
        self.assertEqual(4_000_000_000, u["u32"])
        self.assertEqual(1.5, u["f"])

    def test_typed_numbers_round_trip(self):
        packed = mbp.pack(mbp.typed("a", "I8", -2), mbp.typed("b", "16L", 513), mbp.typed("c", "I32", -5),
                          mbp.typed("d", "32", 4_000_000_000), mbp.typed("e", "F", 1.5), mbp.typed("n", "N", 2.0))
        u = mbp.unpack(packed)
        self.assertEqual({"a": -2, "b": 513, "c": -5, "d": 4_000_000_000, "e": 1.5, "n": 2}, u.fields)

    def test_rejects_bad_meta(self):
        self.assertIsNone(mbp.unpack(b"\x01\x02"))
        self.assertIsNone(mbp.unpack(b"abc\x00\x00"))
        meta = b'[["x","S",0,9]]'  # field past the data area
        self.assertIsNone(mbp.unpack(b"abc" + meta + bytes([0, len(meta)])))
        meta16 = b'[["x","16",0,1]]'  # typed field with the wrong width
        self.assertIsNone(mbp.unpack(b"ab" + meta16 + bytes([0, len(meta16)])))
        bad_json = b'[["x","O",0,2]]'
        self.assertIsNone(mbp.unpack(b"{x" + bad_json + bytes([0, len(bad_json)])))
        bool_name = b'[[true,"S",0,1]]'
        self.assertIsNone(mbp.unpack(b"a" + bool_name + bytes([0, len(bool_name)])))

    def test_hash_names_stay_out_of_meta(self):
        self.assertEqual(b"\xd0ab", mbp.pack(mbp.u8("#t", 0xD0), mbp.string("#s", "ab")))
        self.assertIsNone(mbp.unpack(b"\xd0ab"))

    def test_number_text_matches_js_string(self):
        self.assertEqual("3", mbp.number_text(3.0))
        self.assertEqual("3.5", mbp.number_text(3.5))
        self.assertEqual("-0.25", mbp.number_text(-0.25))
        self.assertEqual("NaN", mbp.number_text(math.nan))
        self.assertEqual("Infinity", mbp.number_text(math.inf))

    def test_unsupported_value(self):
        with self.assertRaises(TypeError):
            mbp.value("x", object())


class QuotaTest(unittest.TestCase):
    def test_quota_matches_js_table(self):
        self.assertEqual(1500, signal_size(10))
        self.assertEqual(255, signal_size(1))
        self.assertEqual(1_048_576, signal_size(3))
        self.assertEqual(20 * 1_048_576, signal_size(255))

    def test_quota_packet_gives_max_message_bytes(self):
        self.assertEqual(262_144, max_message_bytes(bytes([0xC6, 0, 0, 4, 0])))
        self.assertIsNone(max_message_bytes(bytes([0xC6, 0, 0, 0, 0])))
        self.assertEqual(0xFFFFFFFF, max_message_bytes(bytes([0xC6, 255, 255, 255, 255])))


if __name__ == "__main__":
    unittest.main()
