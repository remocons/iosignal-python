# Boho against a session and packs recorded from the JS boho (tests/fixtures/js-vectors.json) and the
# legacy 2.1.3 fixture of boho. Random nonces and the clock are fed back from the recorded packets,
# so Python must produce the same bytes as JS.
import json
import os
import unittest

from _path import iosignal  # noqa: F401
from iosignal import boho
from iosignal.boho import Boho

HERE = os.path.dirname(__file__)
with open(os.path.join(HERE, "fixtures", "js-vectors.json"), encoding="utf-8") as f:
    VECTORS = json.load(f)
SESSION = VECTORS["bohoSession"]
PACKS = VECTORS["bohoPacks"]
LEGACY = {
    "key": "compatibility-test-key",
    "plaintext": "Boho 2.1.3 compatibility",
    "packet": "b618000000eec5b46ab80201006b0e83fa12895eb8eaf71eec9fb0b90539ce674dd94110824f8374b15a225069a0ce500f",
}  # boho test/fixtures/legacy-pack.json

h = bytes.fromhex


def clock_of(packet_hex: str) -> int:
    """Epoch ms written in an ENC_488 packet."""
    p = h(packet_hex)
    return int.from_bytes(p[5:9], "little") * 1000 + int.from_bytes(p[9:11], "little")


def feeder(values):
    it = iter(values)
    return lambda *_: next(it)


class SessionReplayTest(unittest.TestCase):
    def client(self, extra_clocks=()):
        req = h(SESSION["authReq"])
        clocks = [clock_of(p) for _, p in SESSION["clientToServer"]] + list(extra_clocks)
        c = Boho(rand=feeder([req[9:13]]), clock=feeder(clocks))
        c.set_id8(SESSION["id"])
        c.set_key(SESSION["key"])
        return c

    def server(self):
        challenge = h(SESSION["challenge"])
        clocks = [int.from_bytes(challenge[1:5], "little") * 1000 + int.from_bytes(challenge[5:7], "little")]
        clocks += [clock_of(p) for _, p in SESSION["serverToClient"]]
        s = Boho(rand=feeder([challenge[9:13], challenge[7:9]]), clock=feeder(clocks))
        s.set_key(SESSION["key"])
        return s

    def test_client_matches_js(self):
        c = self.client()
        self.assertEqual(SESSION["authReq"], c.auth_req(h(SESSION["challenge"])).hex())
        self.assertFalse(c.is_authorized)
        self.assertTrue(c.verify_auth_res(h(SESSION["authRes"])))
        self.assertTrue(c.is_authorized)
        for text, packet in SESSION["serverToClient"]:
            self.assertEqual(text.encode(), c.decrypt_488(h(packet)))
        for text, packet in SESSION["clientToServer"]:
            self.assertEqual(packet, c.encrypt_488(text).hex(), text)

    def test_server_matches_js(self):
        s = self.server()
        self.assertEqual(SESSION["challenge"], s.server_time_nonce().hex())
        self.assertEqual(b"device-0", s.id8_of(h(SESSION["authReq"])))
        self.assertEqual(SESSION["authRes"], s.verify_auth_req(h(SESSION["authReq"])).hex())
        for text, packet in SESSION["clientToServer"]:
            self.assertEqual(text.encode(), s.decrypt_488(h(packet)))
        for text, packet in SESSION["serverToClient"]:
            self.assertEqual(packet, s.encrypt_488(text).hex(), text)

    def test_wrong_key_is_refused(self):
        s = self.server()
        s.server_time_nonce()
        self.assertIsNone(s.verify_auth_req(h(SESSION["wrongAuthReq"])))
        self.assertFalse(s.is_authorized)
        c = Boho(rand=feeder([bytes(4)]))
        c.set_id8("x")
        c.set_key("wrong")
        c.auth_req(h(SESSION["challenge"]))
        self.assertFalse(c.verify_auth_res(h(SESSION["authRes"])))
        self.assertFalse(c.is_authorized)

    def test_tampered_packets_are_refused(self):
        c = self.client()
        c.auth_req(h(SESSION["challenge"]))
        c.verify_auth_res(h(SESSION["authRes"]))
        packet = bytearray(h(SESSION["serverToClient"][1][1]))
        for i in range(len(packet)):
            bad = bytearray(packet)
            bad[i] ^= 0x01
            self.assertIsNone(c.decrypt_488(bytes(bad)), i)
        for n in range(len(packet)):
            self.assertIsNone(c.decrypt_488(bytes(packet[:n])))
        self.assertIsNone(c.decrypt_488(bytes(packet) + b"\x00"))

    def test_nothing_before_login(self):
        c = self.client()
        self.assertIsNone(c.encrypt_488("x"))
        self.assertIsNone(c.decrypt_488(h(SESSION["serverToClient"][1][1])))
        self.assertIsNone(Boho().auth_req(h(SESSION["challenge"])))  # no key

    def test_malformed_challenge(self):
        c = self.client()
        challenge = bytearray(h(SESSION["challenge"]))
        challenge[5:7] = (1000).to_bytes(2, "little")
        self.assertIsNone(c.auth_req(bytes(challenge)))
        self.assertIsNone(c.auth_req(h(SESSION["challenge"])[:12]))

    def test_enc_e2e_header(self):
        c = self.client()
        c.auth_req(h(SESSION["challenge"]))
        c.verify_auth_res(h(SESSION["authRes"]))
        enc = bytearray(h(SESSION["serverToClient"][1][1]))
        enc[0] = boho.ENC_E2E
        self.assertEqual(b"hello", c.decrypt_488(bytes(enc) + b"opaque e2e bytes"))


class LiveRoundTripTest(unittest.TestCase):
    def test_python_login_and_both_directions(self):
        server, client = Boho(), Boho()
        server.set_key("k")
        client.set_id8("dev1")
        client.set_key("k")
        self.assertTrue(client.verify_auth_res(server.verify_auth_req(client.auth_req(server.server_time_nonce()))))
        for size in (0, 1, 31, 32, 33, 1024):
            data = bytes(range(256)) * 4
            data = data[:size]
            self.assertEqual(data, server.decrypt_488(client.encrypt_488(data)))
            self.assertEqual(data, client.decrypt_488(server.encrypt_488(data)))

    def test_counter_wrap_keeps_clock_monotonic(self):
        b = Boho(clock=lambda: 5_000)
        b.copy_key(bytes(32))
        b.is_authorized = True
        b.counter = 0xFFFE
        first = b.encrypt_488("a")
        second = b.encrypt_488("b")  # counter wraps to 0 at the same ms
        self.assertEqual(0xFFFF, int.from_bytes(first[11:13], "little"))
        self.assertEqual(0, int.from_bytes(second[11:13], "little"))
        self.assertEqual(5_001, int.from_bytes(second[5:9], "little") * 1000 + int.from_bytes(second[9:11], "little"))

    def test_keys(self):
        b = Boho()
        with self.assertRaises(ValueError):
            b.set_key("")
        with self.assertRaises(ValueError):
            b.copy_key(b"\x01")
        with self.assertRaises(RuntimeError):
            b.encrypt_pack("x")
        self.assertFalse(b.set_id_key("nodot"))
        self.assertTrue(b.set_id_key("dev.k.ey"))
        self.assertEqual(b"dev\x00\x00\x00\x00\x00", b._id8)
        self.assertEqual(boho.sha256(b"k.ey"), b._key32)


class PackTest(unittest.TestCase):
    def test_js_packs(self):
        b = Boho()
        b.set_key(PACKS["key"])
        for plain, packet in PACKS["packs"]:
            self.assertEqual(h(plain), b.decrypt_pack(h(packet)))
            self.assertEqual(len(h(plain)) + boho.ENC_PACK_META, len(h(packet)))

    def test_js_e2e(self):
        b = Boho()
        b.set_key(PACKS["key"])
        for text, packet in PACKS["e2e"]:
            self.assertEqual(text.encode(), b.decrypt_e2e(h(packet), PACKS["e2eKey"]))
            self.assertIsNone(b.decrypt_pack(h(packet)))  # not the session key

    def test_legacy_213_packet(self):
        b = Boho()
        b.set_key(LEGACY["key"])
        self.assertEqual(LEGACY["plaintext"].encode(), b.decrypt_pack(h(LEGACY["packet"])))

    def test_pack_round_trip_and_rejects(self):
        b = Boho()
        b.set_key("key")
        packet = b.encrypt_pack("test message")
        self.assertEqual(b"test message", b.decrypt_pack(packet))
        for n in range(len(packet)):
            self.assertIsNone(b.decrypt_pack(packet[:n]))
        self.assertIsNone(b.decrypt_pack(packet + b"\x00"))
        wrong_type = bytes([boho.ENC_488]) + packet[1:]
        self.assertIsNone(b.decrypt_pack(wrong_type))

    def test_e2e_leaves_session_state(self):
        server, client = Boho(), Boho()
        server.set_key("k")
        client.set_id8("d")
        client.set_key("k")
        client.verify_auth_res(server.verify_auth_req(client.auth_req(server.server_time_nonce())))
        sealed = client.encrypt_e2e("x", "other")
        self.assertEqual(b"x", server.decrypt_e2e(sealed, "other"))
        self.assertEqual(b"after", server.decrypt_488(client.encrypt_488("after")))


if __name__ == "__main__":
    unittest.main()
