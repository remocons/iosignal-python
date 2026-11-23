# Compares with bytes the JS implementations produced (tests/fixtures/js-vectors.json, from tools/js-vectors.mjs).
import json
import os
import unittest

from _path import iosignal  # noqa: F401
from iosignal import mbp
from iosignal.protocol import decode_signal, encode_payload, signal_packet

with open(os.path.join(os.path.dirname(__file__), "fixtures", "js-vectors.json"), encoding="utf-8") as f:
    VECTORS = json.load(f)


def from_json(v):
    if isinstance(v, dict):
        if set(v) == {"$hex"}:
            return bytes.fromhex(v["$hex"])
        return {k: from_json(x) for k, x in v.items()}
    if isinstance(v, list):
        return [from_json(x) for x in v]
    return v


class JsVectorsTest(unittest.TestCase):
    def test_number_text(self):
        for n, text in VECTORS["numberText"]:
            self.assertEqual(text, mbp.number_text(n), n)

    def test_mba_pack_and_unpack(self):
        for case in VECTORS["mba"]:
            values = from_json(case["args"])
            self.assertEqual(case["hex"], mbp.pack(mbp.args(values)).hex(), values)
            self.assertEqual(values, mbp.unpack(bytes.fromhex(case["hex"])).args, values)

    def test_typed(self):
        fields = VECTORS["typed"]["fields"]
        packed = mbp.pack(*(mbp.typed(name, t, v) for name, t, v in fields))
        self.assertEqual(VECTORS["typed"]["hex"], packed.hex())
        expected = {name: v for name, t, v in fields if "#" not in name}
        self.assertEqual(expected, mbp.unpack(packed).fields)

    def test_signals(self):
        for case in VECTORS["signals"]:
            values = from_json(case["args"])
            packet = signal_packet(case["tag"], *encode_payload(*values))
            self.assertEqual(case["hex"], packet.hex(), case)
            signal = decode_signal(bytes.fromhex(case["hex"]))
            self.assertEqual(case["tag"], signal.tag)
            expected = [str(v) if isinstance(v, (int, float)) and len(values) == 1 else v for v in values]
            self.assertEqual(expected, signal.args, case)


if __name__ == "__main__":
    unittest.main()
