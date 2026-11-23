# The blocking wrapper against the in-memory servers of test_client and test_devices.
import threading
import time
import unittest

from _path import iosignal  # noqa: F401
from iosignal.client import AuthError, PingError, RpcError
from iosignal.protocol import State
from iosignal.sync import IOSignal
from test_client import FakeServer
from test_devices import BusServer


class SyncTest(unittest.TestCase):
    def make(self, server):
        io = IOSignal("ws://test", connect=server.connect)
        io.set("check_period", 0.02)
        io.set("reconnect_base", 0.02)
        self.addCleanup(io.shutdown)
        return io

    def test_ready_signal_queue_and_handler(self):
        server = FakeServer()
        io = self.make(server)
        inbox = io.listen("echo-back")
        seen = []
        io.on("echo-back", lambda s: seen.append(threading.current_thread().name))
        states = io.on_event("state")
        io.open()
        self.assertEqual("?a1", io.ready())
        self.assertEqual(State.READY, io.state)
        time.sleep(0.05)
        self.assertTrue(io.signal("echo", "hi"))
        got = inbox.get(timeout=2)
        self.assertEqual(("echo-back", "hi"), (got.tag, got.text))
        self.assertEqual(["iosignal"], seen)  # handlers run on the client's thread
        self.assertEqual(State.CONNECTING, states.get(timeout=1))
        self.assertEqual(["echo-back"], io.subscriptions)
        self.assertEqual("ws://test", io.url)

    def test_handler_may_send(self):
        server = FakeServer()
        io = self.make(server)
        replies = io.listen("echo-back")
        io.on_event("ready", lambda cid: io.signal("echo", "from handler " + cid))
        io.open()
        io.ready()
        self.assertEqual("from handler ?a1", replies.get(timeout=2).text)

    def test_login_and_calls(self):
        server = FakeServer()
        io = self.make(server)
        io.auth("dev1", "dev1").open()
        self.assertEqual("dev1", io.ready())
        self.assertTrue(io.is_encrypted)
        self.assertEqual({"args": [1, "a"]}, io.call("svc", "echo", 1, "a").body)
        with self.assertRaises(RpcError):
            io.call("svc", "nope")

    def test_ping_server(self):
        server = FakeServer()
        io = self.make(server)
        with self.assertRaises(PingError):
            io.ping_server()
        io.open()
        io.ready()
        self.assertGreaterEqual(io.ping_server(), 0.0)

    def test_wrong_key(self):
        io = self.make(FakeServer())
        io.auth("dev1", "x").open()
        with self.assertRaises(AuthError):
            io.ready()

    def test_device_discover_command(self):
        server = BusServer()
        dev = self.make(server)
        lamp = dev.device("전등:방", type="light.1", state={"on": "0"})
        dev.open()
        dev.ready()
        app = self.make(server)
        app.open()
        app.ready()
        found = app.discover(wait=0.2)
        self.assertEqual(["전등"], [d.name for d in found])
        states = app.listen(found[0].id + "@$state")
        self.assertEqual("on=0", states.get(timeout=1).text)
        app.command(found[0], "toggle")
        self.assertEqual("on=1", states.get(timeout=1).text)
        dev.run(lambda: lamp.set(on=0))
        self.assertEqual("on=0", states.get(timeout=1).text)

    def test_shutdown(self):
        io = IOSignal("ws://test", connect=FakeServer().connect)
        io.open()
        io.ready()
        io.shutdown()
        io.shutdown()  # twice is fine
        self.assertFalse(io._thread.is_alive())
        with self.assertRaises(RuntimeError):
            io.signal("x", "y")

    def test_context_manager(self):
        with IOSignal("ws://test", connect=FakeServer().connect) as io:
            self.assertTrue(io.ready())
        self.assertFalse(io._thread.is_alive())


if __name__ == "__main__":
    unittest.main()
