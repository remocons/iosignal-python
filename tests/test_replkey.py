# REPL key text -> bytes (replkey.py); web/test/replkey_check.mjs checks web/replclient.js against the same vectors.
import json
import os
import unittest

from _path import iosignal  # noqa: F401
from iosignal import replkey

with open(os.path.join(os.path.dirname(__file__), "fixtures", "repl-key-vectors.json"), encoding="utf-8") as f:
    VECTORS = json.load(f)["cases"]


class ReplKeyTest(unittest.TestCase):
    def test_vectors(self):
        for v in VECTORS:
            with self.subTest(v["name"]):
                self.assertEqual(v["kind"], replkey.kind(v["text"]))
                if "hex" in v:
                    self.assertEqual(v["hex"], replkey.key_bytes(v["text"]).hex())
                else:
                    with self.assertRaises(ValueError):
                        replkey.key_bytes(v["text"])

    def test_new_key(self):
        seen = set()
        for _ in range(50):
            text = replkey.new_key_text()
            self.assertRegex(text, r"^([0-9A-HJKMNP-TV-Z]{5}-){4}[0-9A-HJKMNP-TV-Z]{5}$")
            self.assertEqual("base32", replkey.kind(text))
            b = replkey.key_bytes(text)
            self.assertEqual(16, len(b))
            self.assertEqual(b, replkey.key_bytes(text.lower().replace("-", "")))
            seen.add(text)
        self.assertEqual(50, len(seen))


if __name__ == "__main__":
    unittest.main()
