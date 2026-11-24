# Boho 3 (Ascon) against a session and packs recorded from the JS boho (tests/fixtures/js-vectors.json),
# the shared vectors of the JS boho package (tests/fixtures/boho-vectors.json, a copy of the boho
# source's test/fixtures/boho-vectors.json) and official Ascon known answers. Random nonces and the clock
# are fed back from the recorded packets, so Python must produce the same bytes as JS.
import json
import os
import unittest

from _path import iosignal  # noqa: F401
from iosignal import ascon, boho
from iosignal.boho import Boho

HERE = os.path.dirname(__file__)
with open(os.path.join(HERE, "fixtures", "js-vectors.json"), encoding="utf-8") as f:
    VECTORS = json.load(f)
with open(os.path.join(HERE, "fixtures", "boho-vectors.json"), encoding="utf-8") as f:
    SHARED = json.load(f)
SESSION = VECTORS["bohoSession"]
PACKS = VECTORS["bohoPacks"]

h = bytes.fromhex


def clock_of(packet_hex: str) -> int:
    """Epoch ms written in an ENC or ENC_PACK packet."""
    p = h(packet_hex)
    return int.from_bytes(p[5:9], "little") * 1000 + int.from_bytes(p[9:11], "little")


def feeder(values):
    it = iter(values)
    return lambda *_: next(it)


class AsconTest(unittest.TestCase):
    KEY, NONCE = bytes(range(16)), bytes(range(16, 32))

    def test_known_answers(self):
        # ascon-c LWC_AEAD_KAT_128_128.txt Count = 1, 34, 100, 1089 and LWC_CXOF_KAT_128_512.txt Count = 1, 307
        for pt, ad, ct in [
            ("", "", "4F9C278211BEC9316BF68F46EE8B2EC6"),
            ("20", "", "E8DD576ABA1CD3E6FC704DE02AEDB79588"),
            ("202122", "", "E8C3DEAF8E12816B8EDF39AD1571A9492B7CA2"),
            ("202122232425262728292A2B2C2D2E2F303132333435363738393A3B3C3D3E3F",
             "303132333435363738393A3B3C3D3E3F404142434445464748494A4B4C4D4E4F",
             "CB34D04660A66DBFBE9C856601F5B8AA51A499B55AC8F7FBEFBC331A613EE9CDFD191750A47F211C0A15ED28173D7CAA"),
        ]:
            c, t = ascon.encrypt(self.KEY, self.NONCE, h(pt), h(ad))
            self.assertEqual(ct, (c + t).hex().upper())
            self.assertEqual(h(pt), ascon.decrypt(self.KEY, self.NONCE, c, h(ad), t))
            self.assertIsNone(ascon.decrypt(self.KEY, self.NONCE, c, h(ad), bytes([t[0] ^ 1]) + t[1:]))
        self.assertEqual("4F50159EF70BB3DAD8807E034EAEBD44C4FA2CBBC8CF1F05511AB66CDCC529905CA12083FC186AD899B270B1473DC5F7EC88D1052082DCDFE69FB75D269E7B74",
                         ascon.cxof128(b"", 64, b"").hex().upper())
        self.assertEqual("75BA4455E500E37946C81FDDF6A5E168A94F18D1B31C7927D109736A8A33887E3D7E189E569ACD8508F075E06E89C72877AFB39133EFE2A518DF33BD6D2499CA",
                         ascon.cxof128(bytes(range(9)), 64, bytes(range(16, 25))).hex().upper())

    def test_native_self_check(self):
        # The import-time check that decides ascon.NATIVE: the pure Python permutation passes it, wrong ones do not.
        self.assertTrue(ascon._native_ok(lambda st, start: ascon._permute_bytes(st, 12 - start)))
        self.assertFalse(ascon._native_ok(lambda st, start: None))
        self.assertFalse(ascon._native_ok(lambda st, start: ascon._permute_bytes(st, 11 - start)))  # a round short

    def test_round_trips_and_tag_lengths(self):
        for n in range(0, 50):
            c, t = ascon.encrypt(self.KEY, self.NONCE, bytes(range(n)), b"ad", 8)
            self.assertEqual(8, len(t))
            self.assertEqual(bytes(range(n)), ascon.decrypt(self.KEY, self.NONCE, c, b"ad", t))
        self.assertIsNone(ascon.decrypt(self.KEY, self.NONCE, b"", b"", b"abc"))


class SessionReplayTest(unittest.TestCase):
    def client(self):
        req = h(SESSION["authReq"])
        clocks = [clock_of(p) for _, p in SESSION["clientToServer"]]
        c = Boho(rand=feeder([req[9:17]]), clock=feeder(clocks))
        c.set_id8(SESSION["id"])
        c.set_key(SESSION["key"])
        return c

    def server(self):
        challenge = h(SESSION["challenge"])
        clocks = [int.from_bytes(challenge[1:5], "little") * 1000 + int.from_bytes(challenge[5:7], "little")]
        clocks += [clock_of(p) for _, p in SESSION["serverToClient"]]
        s = Boho(rand=feeder([challenge[7:13]]), clock=feeder(clocks))
        s.copy_key(boho.derive_key(SESSION["key"]))
        return s

    def test_client_matches_js(self):
        c = self.client()
        self.assertEqual(SESSION["authReq"], c.auth_req(h(SESSION["challenge"])).hex())
        self.assertFalse(c.is_authorized)
        self.assertTrue(c.verify_auth_res(h(SESSION["authRes"])))
        self.assertTrue(c.is_authorized)
        for text, packet in SESSION["serverToClient"]:
            self.assertEqual(text.encode(), c.decrypt_session(h(packet)))
        for text, packet in SESSION["clientToServer"]:
            self.assertEqual(packet, c.encrypt_session(text).hex(), text)

    def test_server_matches_js(self):
        s = self.server()
        self.assertEqual(SESSION["challenge"], s.server_time_nonce().hex())
        self.assertEqual(SESSION["authRes"], s.verify_auth_req(h(SESSION["authReq"])).hex())
        for text, packet in SESSION["clientToServer"]:
            self.assertEqual(text.encode(), s.decrypt_session(h(packet)))
        for text, packet in SESSION["serverToClient"]:
            self.assertEqual(packet, s.encrypt_session(text).hex(), text)

    def test_refusals(self):
        s = self.server()
        s.server_time_nonce()
        self.assertIsNone(s.verify_auth_req(h(SESSION["wrongAuthReq"])))
        self.assertFalse(s.is_authorized)
        c = self.client()
        self.assertIsNone(c.encrypt_session("x"))  # before login
        c.auth_req(h(SESSION["challenge"]))
        self.assertFalse(c.verify_auth_res(bytes([boho.AUTH_RES]) + bytes(16)))
        self.assertTrue(c.verify_auth_res(h(SESSION["authRes"])))
        packet = h(SESSION["serverToClient"][1][1])
        for i in range(len(packet)):
            bad = bytearray(packet)
            bad[i] ^= 1
            self.assertIsNone(c.decrypt_session(bytes(bad)), i)
        for n in range(len(packet)):
            self.assertIsNone(c.decrypt_session(packet[:n]))
        self.assertIsNone(c.decrypt_session(packet + b"\x00"))
        self.assertIsNone(c.decrypt_session(h(SESSION["clientToServer"][1][1])), "own direction refused")
        self.assertIsNone(c.encrypt_session("x", 0xB8), "a Boho 2.x type")

    def test_e2e_header_and_live_session(self):
        server, client = Boho(), Boho()
        server.set_key("k")
        client.set_id_key("dev.k")
        self.assertTrue(client.verify_auth_res(server.verify_auth_req(client.auth_req(server.server_time_nonce()))))
        enc = client.encrypt_session(b"hello", boho.ENC_E2E)
        self.assertEqual(b"hello", server.decrypt_session(enc + b"opaque e2e bytes"))
        for data in [b"", b"a" * 15, b"b" * 16, bytes(range(256)) * 4]:
            self.assertEqual(data, server.decrypt_session(client.encrypt_session(data)))
            self.assertEqual(data, client.decrypt_session(server.encrypt_session(data)))


class PackReplayTest(unittest.TestCase):
    def test_packs_match_js(self):
        packs = PACKS["packs"] + PACKS["e2e"]
        sender = h(packs[0][1])[13:21]
        b = Boho(rand=feeder([sender]), clock=feeder([clock_of(p) for _, p in packs]))
        b.set_key(PACKS["key"])
        for plain, packet in PACKS["packs"]:
            self.assertEqual(packet, b.encrypt_pack(h(plain)).hex())
            self.assertEqual(h(plain), b.decrypt_pack(h(packet)))
        for text, packet in PACKS["e2e"]:
            self.assertEqual(packet, b.encrypt_e2e(text, PACKS["e2eKey"]).hex())
            self.assertEqual(text.encode(), b.decrypt_e2e(h(packet), PACKS["e2eKey"]))
            self.assertIsNone(b.decrypt_e2e(h(packet), "other"))


class SharedVectorsTest(unittest.TestCase):
    """The same answers as JS, C++ (Arduino) and Kotlin for the boho source's test/fixtures/boho-vectors.json."""

    def test_copy_is_current(self):
        source = os.path.join(HERE, "..", "..", "_boho", "test", "fixtures", "boho-vectors.json")
        if not os.path.exists(source):
            self.skipTest("the boho source is not next to this repository")
        with open(source, encoding="utf-8") as f:
            self.assertEqual(json.load(f), SHARED, "copy the boho source's test/fixtures/boho-vectors.json again")

    def login(self, l):
        c = Boho()
        c.set_id8(l["id"])
        c.set_key(l["key"])
        req = c.auth_req(h(l["challenge"]), h(l["clientNonce"]))
        s = Boho()
        s.copy_key(boho.derive_key(l["key"]))
        res = s.verify_auth_req(req, h(l["challenge"]))
        return c, s, req, res

    def test_keys_logins_messages_packs(self):
        for k in SHARED["keys"]:
            self.assertEqual(k["derived"], boho.derive_key(k["key"]).hex())
        for l in SHARED["logins"]:
            c, s, req, res = self.login(l)
            self.assertEqual(l["authReq"], req.hex())
            self.assertEqual(l["authRes"], res.hex())
            self.assertTrue(c.verify_auth_res(res))
        c, s, _, _ = self.login(SHARED["logins"][0])
        c.verify_auth_res(h(SHARED["logins"][0]["authRes"]))
        for m in SHARED["messages"]:
            side = c if m["dir"] == 2 else s
            self.assertEqual(m["plaintext"], side.decrypt_session(h(m["packet"])).hex())
        for p in SHARED["packs"]:
            self.assertEqual(p["plaintext"], Boho().decrypt_e2e(h(p["packet"]), p["key"]).hex())

    def test_invalid(self):
        sessions = {}
        for l in SHARED["logins"]:
            c, s, _, res = self.login(l)
            c.verify_auth_res(res)
            sessions[l["sessionKey"]] = (c, s)
        for case in SHARED["invalid"]:
            kind, packet = case["kind"], h(case["packet"])
            if kind == "message":
                c, s = sessions[case["sessionKey"]]
                self.assertIsNone((c if case["dir"] == 2 else s).decrypt_session(packet), case["reason"])
            elif kind == "pack":
                self.assertIsNone(Boho().decrypt_e2e(packet, case["key"]), case["reason"])
            elif kind == "authReq":
                s = Boho()
                s.copy_key(boho.derive_key(case["key"]))
                self.assertIsNone(s.verify_auth_req(packet, h(case["challenge"])), case["reason"])
            elif kind == "authRes":
                l = SHARED["logins"][case["sessionOf"]]
                c = Boho()
                c.set_id8(l["id"])
                c.set_key(l["key"])
                c.auth_req(h(l["challenge"]), h(l["clientNonce"]))
                self.assertFalse(c.verify_auth_res(packet), case["reason"])


if __name__ == "__main__":
    unittest.main()
