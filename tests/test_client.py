# Client behavior against an in-memory server built on the Python Boho server side (no network).
import asyncio
import unittest

from _path import iosignal  # noqa: F401
from iosignal import mbp
from iosignal.boho import Boho
from iosignal.client import AuthError, IOSignal, PingError, RpcError, reconnect_delay
from iosignal.protocol import IOMsg, PayloadType, State, decode_signal, encode_payload, signal_packet


class FakeTransport:
    def __init__(self, server, url):
        self.server, self.url = server, url
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.closed = False
        self.boho = None  # server-side session when logged in
        self.cid = None

    async def send(self, data):
        if self.closed:
            raise ConnectionError("closed")
        self.server.handle(self, bytes(data))

    async def recv(self):
        item = await self.inbox.get()
        if item is None:
            raise ConnectionError("closed by server")
        return item

    async def close(self):
        if not self.closed:
            self.closed = True
            self.inbox.put_nowait(None)
            self.server.conns.discard(self)

    def push(self, data):
        """Server -> client, encrypted when the connection is logged in."""
        if self.boho is not None:
            data = self.boho.encrypt_488(data)
        self.inbox.put_nowait(data)


class FakeServer:
    """Enough of the iosignal server: hello, CID, idKey login (key = ID), signals, subscriptions, ping, calls."""

    def __init__(self, auth=True, hello=True, answer=True):
        self.auth, self.hello, self.answer = auth, hello, answer
        self.conns: set[FakeTransport] = set()
        self.urls = []
        self.received = []  # (conn, plaintext packet)
        self.subscribed = []
        self.next_cid = 0
        self.refuse = 0

    async def connect(self, url):
        self.urls.append(url)
        if self.refuse:
            self.refuse -= 1
            raise ConnectionRefusedError("refused")
        t = FakeTransport(self, url)
        self.conns.add(t)
        if self.hello:
            t.challenge_boho = Boho()
            t.inbox.put_nowait(t.challenge_boho.server_time_nonce())
        return t

    def handle(self, t, data):
        if not self.answer:
            return
        if data[0] == IOMsg.ENC_488:
            data = t.boho.decrypt_488(data)
            assert data is not None, "server could not decrypt"
        self.received.append((t, data))
        kind = data[0]
        if kind == IOMsg.CID_REQ:
            if t.cid is None:
                self.next_cid += 1
                t.cid = f"?a{self.next_cid}"
            t.push(bytes([IOMsg.CID_RES]) + t.cid.encode())
        elif kind == IOMsg.AUTH_REQ:
            b = t.challenge_boho
            login = b.id8_of(data).rstrip(b"\x00")
            b.set_key(login)
            res = b.verify_auth_req(data) if self.auth else None
            if res is None:
                t.inbox.put_nowait(bytes([IOMsg.AUTH_FAIL]))
            else:
                t.inbox.put_nowait(res)
                t.boho = b
                t.cid = login.decode()
        elif kind == IOMsg.SUBSCRIBE:
            self.subscribed.append(data[2:2 + data[1]].decode())
        elif kind == IOMsg.PING:
            t.push(bytes([IOMsg.PONG]))
        elif kind == IOMsg.CALL:
            req = mbp.unpack(data)
            if req["topic"] == "echo":
                body = {"args": req.args}
                status = 0
            elif req["topic"] == "slow":
                return
            else:
                body, status = "TEST_FAIL", 200
            t.push(mbp.pack(mbp.u8("#t", IOMsg.RESPONSE_MBP), mbp.u8("status", status), mbp.u16("mid", req["mid"]),
                            mbp.value("body", body)))
        elif kind == IOMsg.SIGNAL:
            sig = decode_signal(data)
            # Echo channel: what is sent to "echo" comes back on "echo-back".
            if sig.tag == "echo":
                t.push(signal_packet("echo-back", sig.type, data[3 + data[1]:]))

    def drop_all(self):
        for t in list(self.conns):
            t.closed = True
            t.inbox.put_nowait(None)
        self.conns.clear()

    def signals(self, conn=None):
        return [decode_signal(d) for c, d in self.received if d[0] == IOMsg.SIGNAL and (conn is None or c is conn)]


async def until(predicate, timeout=2.0):
    end = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > end:
            raise AssertionError("condition not met")
        await asyncio.sleep(0.005)


class ClientTest(unittest.IsolatedAsyncioTestCase):
    def make(self, server, **periods):
        io = IOSignal("ws://test", connect=server.connect)
        io.check_period = periods.get("check", 0.02)
        io.reconnect_base = periods.get("base", 0.02)
        for k in ("ping_period", "pong_timeout", "hello_timeout", "call_timeout"):
            if k in periods:
                setattr(io, k, periods[k])
        self.errors = []
        io.on_event("error", self.errors.append)
        self.addAsyncCleanup(io.aclose)
        return io

    async def test_anonymous_ready_subscribes_and_dispatches(self):
        server = FakeServer()
        io = self.make(server)
        got, messages, states = [], [], []
        io.listen("echo-back", got.append)
        io.on_event("message", messages.append)
        io.on_event("state", states.append)
        io.open()
        cid = await io.ready()
        self.assertEqual("?a1", cid)
        self.assertFalse(io.is_encrypted)
        await until(lambda: server.subscribed == ["echo-back"])
        self.assertTrue(io.signal("echo", "hi"))
        await until(lambda: got)
        self.assertEqual(("echo-back", "hi"), (got[0].tag, got[0].text))
        self.assertIs(got[0], messages[0])
        self.assertEqual([State.CONNECTING, State.OPEN, State.SERVER_READY, State.READY], states)
        self.assertEqual([], self.errors)

    async def test_payload_types_on_the_wire(self):
        server = FakeServer()
        io = self.make(server)
        io.open()
        await io.ready()
        io.signal("room")
        io.signal("room", 42)
        io.signal("room", {"a": 1})
        io.signal("room", "x", b"\x01")
        await until(lambda: len(server.signals()) == 4)
        s = server.signals()
        self.assertEqual([PayloadType.EMPTY, PayloadType.TEXT, PayloadType.OBJECT, PayloadType.MBA], [x.type for x in s])
        self.assertEqual(["x", b"\x01"], s[3].args)

    async def test_login_encrypts_both_ways(self):
        server = FakeServer()
        io = self.make(server)
        io.auth("dev1", "dev1")
        got = []
        io.listen("echo-back", got.append)
        io.open()
        self.assertEqual("dev1", await io.ready())
        self.assertTrue(io.is_authorized and io.is_encrypted)
        io.signal("echo", "안녕")
        await until(lambda: got)
        self.assertEqual("안녕", got[0].text)
        self.assertIn("echo-back", server.subscribed)

    async def test_id_dot_key_form(self):
        server = FakeServer()
        io = self.make(server)
        io.auth("dev2.dev2")
        self.assertEqual("dev2", io.auth_id)
        io.open()
        self.assertEqual("dev2", await io.ready())
        with self.assertRaises(ValueError):
            io.auth("nodot")

    async def test_wrong_key(self):
        server = FakeServer()
        io = self.make(server)
        io.auth("dev1", "nope")
        io.open()
        with self.assertRaises(AuthError):
            await io.ready()
        self.assertEqual(State.AUTH_FAIL, io.state)
        self.assertEqual("", io.cid)

    async def test_server_without_login_answers_auth_fail(self):
        server = FakeServer(auth=False)
        io = self.make(server)
        io.auth("dev1", "dev1")
        io.open()
        with self.assertRaises(AuthError):
            await io.ready()

    async def test_login_on_open_socket(self):
        server = FakeServer()
        io = self.make(server)
        io.open()
        self.assertEqual("?a1", await io.ready())
        io.login("dev3", "dev3")
        await until(lambda: io.cid == "dev3")
        self.assertTrue(io.is_encrypted)

    async def test_calls(self):
        server = FakeServer()
        io = self.make(server, call_timeout=0.1)
        with self.assertRaises(RpcError):
            await io.call("svc", "echo")  # not connected
        io.open()
        await io.ready()
        res = await io.call("svc", "echo", "a", 2, True)
        self.assertTrue(res.ok)
        self.assertEqual({"args": ["a", 2, True]}, res.body)
        with self.assertRaises(RpcError) as failed:
            await io.call("svc", "nope")
        self.assertEqual(200, failed.exception.response.status)
        self.assertEqual("TEST_FAIL", failed.exception.response.body)
        with self.assertRaises(RpcError) as slow:
            await io.call("svc", "slow")
        self.assertEqual("timeout", str(slow.exception))

    async def test_pending_call_fails_when_connection_closes(self):
        server = FakeServer()
        io = self.make(server)
        io.open()
        await io.ready()
        call = asyncio.ensure_future(io.call("svc", "slow", timeout=5))
        await asyncio.sleep(0.01)
        io.close()
        with self.assertRaises(RpcError) as closed:
            await call
        self.assertEqual("Connection closed", str(closed.exception))

    async def test_reconnect_resubscribes(self):
        server = FakeServer()
        io = self.make(server)
        io.subscribe("a,b")
        io.open()
        await io.ready()
        await until(lambda: server.subscribed == ["a,b"])
        server.drop_all()
        await io.wait_for(State.CLOSED, timeout=1)
        self.assertEqual("", io.cid)
        await io.wait_for(State.READY, timeout=2)
        await until(lambda: server.subscribed == ["a,b", "a,b"])
        self.assertEqual("?a2", io.cid)

    async def test_connect_failure_retries(self):
        server = FakeServer()
        server.refuse = 2
        io = self.make(server)
        io.open()
        await io.ready(timeout=3)
        self.assertEqual(3, len(server.urls))
        self.assertTrue(any(isinstance(e, ConnectionRefusedError) for e in self.errors))

    async def test_silent_server_is_abandoned_after_ping(self):
        server = FakeServer()
        io = self.make(server, ping_period=0.05, pong_timeout=0.1)
        io.open()
        await io.ready()
        await asyncio.sleep(0.15)  # PINGs are answered: stays
        self.assertEqual(State.READY, io.state)
        server.answer = False
        await io.wait_for(State.CLOSED, timeout=2)
        self.assertTrue(any("no reply" in str(e) for e in self.errors))
        server.answer = True
        await io.wait_for(State.READY, timeout=2)

    async def test_ping_server(self):
        server = FakeServer()
        io = self.make(server)
        with self.assertRaises(PingError) as e:
            await io.ping_server()
        self.assertEqual("NOT_READY", e.exception.code)
        io.open()
        await io.ready()
        ms = await io.ping_server()
        self.assertIsInstance(ms, float)
        self.assertGreaterEqual(ms, 0.0)
        for bad in (0, 61, "1", True):
            with self.assertRaises(PingError) as e:
                await io.ping_server(bad)
            self.assertEqual("INVALID_ARGUMENT", e.exception.code)

        server.answer = False
        first = asyncio.ensure_future(io.ping_server(0.1))
        await asyncio.sleep(0.01)
        with self.assertRaises(PingError) as e:
            await io.ping_server()
        self.assertEqual("BUSY", e.exception.code)
        with self.assertRaises(PingError) as e:
            await first
        self.assertEqual("TIMEOUT", e.exception.code)

        waiting = asyncio.ensure_future(io.ping_server(5))
        await asyncio.sleep(0.01)
        io.close()
        with self.assertRaises(PingError) as e:
            await waiting
        self.assertEqual("CONNECTION_CLOSED", e.exception.code)
        server.answer = True
        await io.wait_for(State.READY, timeout=2)
        self.assertGreaterEqual(await io.ping_server(), 0.0)  # the guard was released

    async def test_logged_in_ws_sends_nothing_plain_before_ready(self):
        server = FakeServer()
        io = self.make(server)
        sent_at_open = []
        io.on_event("state", lambda s: sent_at_open.append(io.signal("room#t", "secret"))
                    if s == State.OPEN else None)
        io.auth("dev1", "dev1").open()
        await io.ready()
        self.assertEqual([False], sent_at_open)
        self.assertFalse(any(b"secret" in p for _, p in server.received))
        self.assertTrue(io.signal("room#t", "after"))

    async def test_ready_timeout_is_builtin_timeout_error(self):
        server = FakeServer(hello=False)
        io = self.make(server)
        io.open()
        with self.assertRaises(TimeoutError):
            await io.ready(timeout=0.05)

    async def test_ping_server_waits_past_earlier_pongs(self):
        server = FakeServer()
        io = self.make(server)
        io.open()
        await io.ready()
        server.answer = False
        io.ping()  # a keepalive PING still on the way
        first = asyncio.ensure_future(io.ping_server(2))
        await asyncio.sleep(0.01)
        conn = next(iter(server.conns))
        conn.push(bytes([IOMsg.PONG]))  # answers the earlier PING only
        await asyncio.sleep(0.05)
        self.assertFalse(first.done())
        conn.push(bytes([IOMsg.PONG]))
        self.assertGreaterEqual(await first, 0.0)

    async def test_over_size_call_says_so(self):
        server = FakeServer()
        io = self.make(server)
        io.open()
        await io.ready()
        io.max_message_bytes, io._quota_told = 100, True
        with self.assertRaises(RpcError) as e:
            await io.call("svc", "echo", b"x" * 200)
        self.assertEqual("over size", str(e.exception))
        io.close()
        with self.assertRaises(RpcError) as e:
            await io.call("svc", "echo", 1)
        self.assertEqual("not connected", str(e.exception))

    async def test_no_hello_is_abandoned(self):
        server = FakeServer(hello=False)
        io = self.make(server, hello_timeout=0.05)
        io.open()
        await io.wait_for(State.CLOSED, timeout=1)
        self.assertTrue(any("no reply" in str(e) for e in self.errors))

    async def test_stop_does_not_reconnect(self):
        server = FakeServer()
        io = self.make(server)
        io.open()
        await io.ready()
        io.stop()
        await asyncio.sleep(0.1)
        self.assertEqual(State.STOP, io.state)
        self.assertEqual(1, len(server.urls))
        self.assertFalse(io.signal("x", "y"))

    async def test_auth_clear_stops(self):
        server = FakeServer()
        io = self.make(server)
        io.auth("dev1", "dev1")
        io.open()
        await io.ready()
        next(iter(server.conns)).push(bytes([IOMsg.AUTH_CLEAR]))
        await io.wait_for(State.STOP, timeout=1)
        self.assertIsNone(io.auth_id)

    async def test_redirect_then_back_to_own_url(self):
        server = FakeServer()
        io = self.make(server)
        io.open()
        await io.ready()
        next(iter(server.conns)).push(bytes([IOMsg.SERVER_REDIRECT]) + b"ws://broker")
        await until(lambda: io.connected_url == "ws://broker" and io.state == State.READY)
        self.assertEqual("ws://test", io.url)
        server.drop_all()
        await until(lambda: io.connected_url == "ws://test" and io.state == State.READY)

    async def test_quota_and_over_size(self):
        server = FakeServer()
        io = self.make(server)
        over = []
        io.on_event("over_size", lambda: over.append(1))
        io.open()
        await io.ready()
        next(iter(server.conns)).push(bytes([IOMsg.QUOTA]) + (20).to_bytes(4, "little"))
        await until(lambda: io.max_message_bytes == 20)
        self.assertTrue(io.signal("t", "short"))
        self.assertFalse(io.signal("t", "x" * 30))
        self.assertEqual([1], over)

    async def test_direct_and_handler_errors(self):
        server = FakeServer()
        io = self.make(server)
        seen, directs, async_seen = [], [], []

        def bad(sig):
            raise RuntimeError("handler bug")

        async def later(sig):
            await asyncio.sleep(0)
            async_seen.append(sig.tag)

        io.on("@", lambda s: seen.append(("@", s.tag)))
        io.on("@set", bad)
        io.on("@set", lambda s: seen.append(("@set", s.text)))
        io.on("@set", later)
        io.on_event("direct", directs.append)
        io.open()
        await io.ready()
        conn = next(iter(server.conns))
        conn.push(signal_packet("@set", *encode_payload("1")))
        conn.push(signal_packet("@", *encode_payload("2")))
        await until(lambda: len(directs) == 2 and async_seen)
        self.assertEqual([("@", "@set"), ("@set", "1"), ("@", "@")], seen)
        self.assertEqual(["@set"], async_seen)
        self.assertEqual(["handler bug"], [str(e) for e in self.errors])
        self.assertEqual(State.READY, io.state)

    async def test_tag_checks(self):
        io = self.make(FakeServer())
        with self.assertRaises(ValueError):
            io.subscribe("room,@set")
        with self.assertRaises(ValueError):
            io.on("", lambda s: None)
        with self.assertRaises(ValueError):
            io.on_event("nope", lambda: None)

    async def test_iam_and_server_signal_are_parsed(self):
        server = FakeServer()
        io = self.make(server)
        iam, ss = [], []
        io.on_event("iam", iam.append)
        io.on_event("server_signal", ss.append)
        io.open()
        await io.ready()
        conn = next(iter(server.conns))
        conn.push(bytes([IOMsg.IAM_RES]) + b'{"ip":"1.2.3.4"}')
        conn.push(bytes([IOMsg.SERVER_SIGNAL]) + b"plain")
        await until(lambda: iam and ss)
        self.assertEqual({"ip": "1.2.3.4"}, iam[0])
        self.assertEqual("plain", ss[0])

    def test_reconnect_delay(self):
        self.assertEqual(0.5, reconnect_delay(0, 0.5))
        self.assertAlmostEqual(3.0, reconnect_delay(1, 0.5))
        self.assertAlmostEqual(6.0, reconnect_delay(2, 0.5))
        self.assertAlmostEqual(30.0, reconnect_delay(10, 0.5))
        self.assertAlmostEqual(24.0, reconnect_delay(10, 0.0))


if __name__ == "__main__":
    unittest.main()
