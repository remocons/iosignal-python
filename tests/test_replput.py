# Files.put (replhost.py) against a simulated board, no network: pacing below the server's message quota, and
# stopping at once on a quota notice, a refusal from the board, or a board that stops answering. Same cases as
# web/test/put_check.mjs for the web IDE.
import asyncio
import json
import time
import unittest

from _path import iosignal  # noqa: F401
from iosignal.replhost import FsError, ReplSession
from iosignal.replproto import DATA, FS


class FakeIO:
    cid = "me"

    def __init__(self):
        self.events = {}

    def on(self, tag, handler):
        pass

    def on_event(self, name, handler):
        self.events.setdefault(name, []).append(handler)

    def off_event(self, name, handler):
        self.events.get(name, []).remove(handler)

    def emit(self, name, body):
        for h in list(self.events.get(name, [])):
            h(body)


def setup(mode):
    """mode: 'fast' | 'refuse' (offset error after 3 frames) | 'stall' (no acks after 3 frames) | 'quota'."""
    io = FakeIO()
    s = ReplSession(io, "b", bytes(16), forward_secrecy=False)
    st = {"at": 0, "acked": 0, "frames": 0, "times": [], "ops": []}
    loop = asyncio.get_running_loop()

    def later(fn):
        def run():
            fn()
            s.changed.set()
        loop.call_soon(run)

    def send_frame(kind, data=b"", sealer=None):
        if kind == FS:
            req = json.loads(data)
            st["ops"].append(req["op"])
            if req["op"] == "put":
                st["at"] = st["acked"] = 0
                later(lambda: s.files._reply({"op": "put", "ok": True}))
            elif req["op"] == "close":
                later(lambda: s.files._reply({"op": "close", "ok": True, "size": st["at"], "sha256": req["sha256"]}))
        elif kind == DATA:
            st["times"].append(time.monotonic())
            st["frames"] += 1
            if mode == "refuse" and st["frames"] == 3:
                return later(lambda: s.files._reply({"op": "put", "ok": False, "error": "offset 2800, expected 1400"}))
            if mode == "quota" and st["frames"] == 3:
                return later(lambda: io.emit("server_signal", {"event": "quota_exceeded",
                                                               "data": {"kind": "msgPerSec", "limit": 50, "quota": "anonymous"}}))
            if mode == "stall" and st["frames"] >= 3:
                return
            st["at"] += len(data) - 4
            if st["at"] - st["acked"] >= 4096:
                st["acked"] = st["at"]
                at = st["at"]
                later(lambda: s.files._reply({"op": "ack", "at": at}))

    s.send_frame = send_frame
    return s, st, io


DATA_64K = bytes(64 * 1024)


class PutTest(unittest.IsolatedAsyncioTestCase):
    async def test_paced(self):
        s, st, io = setup("fast")
        await s.files.put("/x", DATA_64K, max_frames_per_sec=40)
        t = st["times"]
        most = max(sum(1 for x in t if a <= x < a + 1.0) for a in t)
        self.assertLessEqual(most, 41)
        self.assertEqual(io.events["server_signal"], [])  # the listener is gone

    async def test_stops(self):
        for mode, words, most in (("quota", "quota", 1.0), ("refuse", "went missing", 1.0), ("stall", "did not answer", 2.0)):
            with self.subTest(mode):
                s, st, io = setup(mode)
                t = time.monotonic()
                with self.assertRaises(FsError) as e:
                    await s.files.put("/x", DATA_64K, stall=1.0)
                self.assertIn(words, str(e.exception))
                self.assertLess(time.monotonic() - t, most)
                self.assertIn("abort", st["ops"])
                self.assertEqual(io.events["server_signal"], [])


if __name__ == "__main__":
    unittest.main()
