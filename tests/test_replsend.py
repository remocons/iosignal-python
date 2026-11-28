# ReplSession.send pacing (replhost.py), no network: a long paste goes out as 1 kB frames, a burst of 10, then
# at most about 30 a second; a lone Ctrl-C drops the rest and goes first; drain_input waits for the queue.
# Same rule as web/test/paste_check.mjs for the web IDE.
import asyncio
import time
import unittest

from _path import iosignal  # noqa: F401
from iosignal.replhost import ReplSession
from iosignal.replproto import IN


class FakeIO:
    cid = "me"

    def on(self, tag, handler):
        pass


def session():
    s = ReplSession(FakeIO(), "b", bytes(16), forward_secrecy=False)
    sent = []
    s.send_frame = lambda kind, data=b"", sealer=None: sent.append((kind, time.monotonic(), bytes(data)))
    return s, sent


class SendTest(unittest.IsolatedAsyncioTestCase):
    async def test_paste_paced(self):
        s, sent = session()
        t0 = time.monotonic()
        s.send(b"x" * (100 * 1024))
        self.assertEqual(10, len(sent))  # the burst, at once
        await asyncio.sleep(1.5)
        times = [t for _, t, _ in sent]
        most = max(sum(1 for x in times if a <= x < a + 1.0) for a in times)
        self.assertLessEqual(most, 41)
        self.assertTrue(all(k == IN and len(d) == 1024 for k, _, d in sent))
        self.assertLess(len(sent), 100)
        n = len(sent)
        s.send(b"\x03")  # Ctrl-C jumps the queue and drops the rest
        await asyncio.sleep(0.2)
        self.assertEqual(n + 1, len(sent))
        self.assertEqual(b"\x03", sent[-1][2])
        s.send("1+1\r")  # typing after it goes out at once
        self.assertEqual(b"1+1\r", sent[-1][2])
        self.assertLess(time.monotonic() - t0, 3)

    async def test_drain(self):
        s, sent = session()
        s.send(b"y" * (20 * 1024))
        await s.drain_input()
        self.assertEqual(20, len(sent))
        self.assertEqual(b"y" * (20 * 1024), b"".join(d for _, _, d in sent))

    async def test_close_drops_queue(self):
        s, sent = session()
        s.send(b"z" * (30 * 1024))
        s.close()
        await asyncio.sleep(0.2)
        self.assertEqual(11, len(sent))  # the burst and the close frame, nothing after


if __name__ == "__main__":
    unittest.main()
