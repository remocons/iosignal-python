# The byte layer (mbp, protocol, boho) against the JS vectors, without unittest so it runs on MicroPython too.
#   micropython tests/mpy_vectors.py        (from the repository root; also: python3 tests/mpy_vectors.py)
# Same cases as tests/test_js_vectors.py and tests/test_boho.py, from tests/fixtures/js-vectors.json.
import json
import sys
import time

sys.path.insert(0, "src")

from iosignal import boho, mbp  # noqa: E402
from iosignal.protocol import decode_signal, encode_payload, signal_packet  # noqa: E402

with open("tests/fixtures/js-vectors.json") as f:
    V = json.load(f)

passed = 0
failures = []


def check(name, got, want):
    global passed
    if got == want:
        passed += 1
    else:
        failures.append("%s: got %r, want %r" % (name, got, want))


def from_json(v):
    if isinstance(v, dict):
        if list(v.keys()) == ["$hex"]:
            return bytes.fromhex(v["$hex"])
        return {k: from_json(x) for k, x in v.items()}
    if isinstance(v, list):
        return [from_json(x) for x in v]
    return v


h = bytes.fromhex

# ---- mbp and protocol
for n, text in V["numberText"]:
    check("number_text %r" % (n,), mbp.number_text(n), text)

for case in V["mba"]:
    values = from_json(case["args"])
    check("mba pack %r" % (values,), mbp.pack(mbp.args(values)).hex(), case["hex"])
    check("mba unpack %r" % (values,), mbp.unpack(h(case["hex"])).args, values)

fields = V["typed"]["fields"]
packed = mbp.pack(*[mbp.typed(name, t, v) for name, t, v in fields])
check("typed pack", packed.hex(), V["typed"]["hex"])
check("typed unpack", mbp.unpack(packed).fields, {name: v for name, t, v in fields if "#" not in name})

for case in V["signals"]:
    values = from_json(case["args"])
    check("signal %s %r" % (case["tag"], values), signal_packet(case["tag"], *encode_payload(*values)).hex(), case["hex"])
    s = decode_signal(h(case["hex"]))
    one_number = len(values) == 1 and isinstance(values[0], (int, float)) and not isinstance(values[0], bool)
    check("signal args %s" % case["tag"], s.args, [str(values[0])] if one_number else values)

check("bad meta", mbp.unpack(b"abc" + b'[["x","S",0,9]]' + bytes([0, 15])), None)

# ---- boho: replay the recorded JS session in both roles
S = V["bohoSession"]


def clock_of(hexpacket):
    p = h(hexpacket)
    return int.from_bytes(p[5:9], "little") * 1000 + int.from_bytes(p[9:11], "little")


def feeder(values):
    items = list(values)
    return lambda *_: items.pop(0)


req = h(S["authReq"])
client = boho.Boho(rand=feeder([req[9:17]]), clock=feeder([clock_of(p) for _, p in S["clientToServer"]]))
client.set_id8(S["id"])
client.set_key(S["key"])
check("client auth_req", client.auth_req(h(S["challenge"])).hex(), S["authReq"])
check("client verify_auth_res", client.verify_auth_res(h(S["authRes"])), True)
for text, packet in S["serverToClient"]:
    check("client decrypt %r" % text, client.decrypt_session(h(packet)), text.encode())
for text, packet in S["clientToServer"]:
    check("client encrypt %r" % text, client.encrypt_session(text).hex(), packet)

ch = h(S["challenge"])
server_clocks = [int.from_bytes(ch[1:5], "little") * 1000 + int.from_bytes(ch[5:7], "little")]
server_clocks += [clock_of(p) for _, p in S["serverToClient"]]
server = boho.Boho(rand=feeder([ch[7:13]]), clock=feeder(server_clocks))
server.copy_key(boho.derive_key(S["key"]))
check("server challenge", server.server_time_nonce().hex(), S["challenge"])
check("server auth_res", server.verify_auth_req(req).hex(), S["authRes"])
for text, packet in S["clientToServer"]:
    check("server decrypt %r" % text, server.decrypt_session(h(packet)), text.encode())
for text, packet in S["serverToClient"]:
    check("server encrypt %r" % text, server.encrypt_session(text).hex(), packet)

bad = bytearray(h(S["serverToClient"][1][1]))
bad[-1] ^= 1
check("tampered refused", client.decrypt_session(bytes(bad)), None)
fresh = boho.Boho()
fresh.set_key(S["key"])
check("wrong key refused", fresh.verify_auth_req(h(S["wrongAuthReq"]), ch), None)
check("wrong key not authorized", fresh.is_authorized, False)

P = V["bohoPacks"]
pk = boho.Boho()
pk.set_key(P["key"])
for plain, packet in P["packs"]:
    check("decrypt_pack %d" % (len(plain) // 2), pk.decrypt_pack(h(packet)), h(plain))
for text, packet in P["e2e"]:
    check("decrypt_e2e %r" % text, pk.decrypt_e2e(h(packet), P["e2eKey"]), text.encode())

# A live login with this interpreter's own random source and clock, and its speed.
a, b = boho.Boho(), boho.Boho()
a.set_key("k")
b.set_id8("dev1")
b.set_key("k")
t0 = time.ticks_ms() if hasattr(time, "ticks_ms") else int(time.time() * 1000)
ok = b.verify_auth_res(a.verify_auth_req(b.auth_req(a.server_time_nonce())))
t1 = time.ticks_ms() if hasattr(time, "ticks_ms") else int(time.time() * 1000)
check("live login", ok, True)
check("live 1 kB", a.decrypt_session(b.encrypt_session(bytes(range(256)) * 4)), bytes(range(256)) * 4)
t2 = time.ticks_ms() if hasattr(time, "ticks_ms") else int(time.time() * 1000)
check("live e2e", a.decrypt_e2e(b.encrypt_e2e(b"x", "other"), "other"), b"x")
print("timing: login %d ms, 1 kB encrypt+decrypt %d ms" % (t1 - t0, t2 - t1))

print("%s: %d passed, %d failed" % (sys.implementation.name, passed, len(failures)))
for line in failures:
    print("  FAIL", line)
sys.exit(1 if failures else 0)
