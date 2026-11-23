"""``iosignal-repl``: mpremote for boards on IOSignal (repl.1, sealed with the board's REPL key).

    iosignal-repl find                         boards (micropython.1) on this home network
    iosignal-repl [-b BOARD] repl              interactive REPL; Ctrl-] leaves, Ctrl-C goes to the board
    iosignal-repl [-b BOARD] exec "CODE"       run code, print its output (exit 1 on an exception)
    iosignal-repl [-b BOARD] run FILE.py       run a local file on the board without copying it
    iosignal-repl [-b BOARD] ls [PATH] | cat PATH | get PATH [LOCAL] | put LOCAL [PATH]
                                rm PATH | mkdir PATH | rmdir PATH | mv PATH TO | info | reset
    iosignal-repl [-b BOARD] update [--pkg DIR] [--force] [--no-reset]
                                the board's IOSignal package from web/pkg (tools/build_web_pkg.py), then restart

Options and environment:
    -s/--server URL   IOSIGNAL_SERVER   default wss://io.iosignal.net/ws
    -k/--key FILE     REPL_KEY_FILE     default ~/.iosignal/repl-<board>.key
    -b/--board CID    IOSIGNAL_BOARD    default: the only micropython.1 board found on this network
"""

import argparse
import asyncio
import os
import sys
import time

from . import devices
from .client import IOSignal
from .replhost import FsError, RawRepl, ReplSession, load_key

DEFAULT_SERVER = "wss://io.iosignal.net/ws"
EXIT_KEY = b"\x1d"  # Ctrl-]


class CliError(Exception):
    pass


def is_repl_board(d) -> bool:
    """A MicroPython board with the remote REPL: $ui t=micropython.1, or r=1 when the board is a device."""
    return d.ui.type == "micropython.1" or devices.parse_state(d.ui_text).get("r") == "1"


async def find_boards(io: IOSignal, wait=1.5):
    return [d for d in await devices.discover(io, wait=wait) if is_repl_board(d)]


def key_for(board: str, path: str | None) -> bytes:
    path = path or os.environ.get("REPL_KEY_FILE") or os.path.expanduser("~/.iosignal/repl-%s.key" % board)
    try:
        return load_key(path)
    except OSError:
        raise CliError("no REPL key at %s (give --key FILE or REPL_KEY_FILE)" % path) from None


async def connect(args):
    io = IOSignal(args.server)
    io.on_event("error", lambda e: None)  # connection errors show up as timeouts below
    io.open()
    try:
        await io.ready(15)
    except Exception:  # noqa: BLE001
        await io.aclose()
        raise CliError("server %s did not answer" % args.server) from None
    return io


async def open_session(io, args):
    board = args.board
    if not board:
        found = await find_boards(io)
        if len(found) != 1:
            raise CliError("give --board CID (%s)" % (
                "no micropython.1 board found on this network" if not found else
                "found: " + ", ".join("%s (%s)" % (d.id, d.name) for d in found)))
        board = found[0].id
    s = ReplSession(io, board, key_for(board, args.key))
    if not await s.open(timeout=8):
        raise CliError("board %s did not answer (offline, wrong key, or not this board)" % board)
    return s


# ---- commands

async def cmd_find(io, args):
    found = await find_boards(io, wait=2.0)
    if not found:
        print("no micropython.1 board found on this network")
    for d in found:
        f = d.fields
        extra = ("repl=%s mem=%s ver=%s" % (f.get("repl"), f.get("mem"), f.get("ver")) if d.ui.type == "micropython.1"
                 else "device %s %s" % (d.ui.type, d.state))
        print("%-12s %-24s %-12s %s" % (d.id, d.name, d.category, extra))
    return 0


async def cmd_repl(io, args):
    s = await open_session(io, args)
    out = sys.stdout.buffer
    closed = asyncio.get_running_loop().create_future()
    s.on_output = lambda b: (out.write(b), out.flush())
    s.on_closed = lambda why: closed.done() or closed.set_result(why)
    print("connected to %s (MicroPython %s). Ctrl-] to leave." % (s.board, s.info.get("mpy")), file=sys.stderr)
    if not sys.stdin.isatty():  # piped input: send it, wait until the board goes quiet
        s.send(b"\x02")  # leave raw REPL if an interrupted exec left the board there
        await asyncio.sleep(0.3)
        s.send(sys.stdin.buffer.read().replace(b"\n", b"\r"))
        await _quiet(s, 1.0)
        s.close()
        return 0
    import termios
    import tty
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    loop = asyncio.get_running_loop()
    done = asyncio.Event()

    def on_key():
        data = os.read(fd, 1024)
        if not data or EXIT_KEY in data:
            done.set()
            return
        s.send(data)

    try:
        # Raw mode first: keys typed from now on go to the board as they are (Enter as \r, no local echo).
        tty.setraw(fd)
        loop.add_reader(fd, on_key)
        # Ctrl-B: leave raw REPL if an interrupted exec left the board there (prints the banner otherwise).
        s.send(b"\x02")
        waiter = asyncio.ensure_future(done.wait())
        await asyncio.wait([waiter, closed], return_when=asyncio.FIRST_COMPLETED)
    finally:
        loop.remove_reader(fd)
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
    if closed.done():
        print("\r\nsession closed by the board (%s)" % closed.result(), file=sys.stderr)
        return 1
    s.close()
    print("\r\nleft the REPL", file=sys.stderr)
    return 0


async def _quiet(s, seconds):
    last = len(s.raw)
    while True:
        await asyncio.sleep(seconds)
        if len(s.raw) == last:
            return
        last = len(s.raw)


async def exec_code(s, code: bytes, timeout=None) -> int:
    """Raw REPL: stream stdout as it comes, the exception to stderr; 1 when the code raised."""
    sys.stdout.flush()  # text printed before goes out before the board's bytes
    r = RawRepl(s)
    await r.enter()
    r.pos = len(s.raw)
    s.send(code + b"\x04")
    await r.read_until(b"OK", 20)
    start = r.pos
    out = sys.stdout.buffer
    written = start
    deadline = time.monotonic() + timeout if timeout else None
    while True:  # stdout up to the first \x04, as it arrives
        end = s.raw.find(b"\x04", start)
        upto = end if end >= 0 else len(s.raw)
        if upto > written:
            out.write(s.raw[written:upto])
            out.flush()
            written = upto
        if end >= 0:
            break
        if deadline and time.monotonic() > deadline:
            raise CliError("timed out")
        await s.wait(lambda: len(s.raw) > written, 3600)
    r.pos = end + 1
    err = (await r.read_until(b"\x04", 30))[:-1]
    await r.read_until(b">", 10)
    await r.exit()
    if err:
        sys.stderr.write(err.decode("utf-8", "replace"))
        return 1
    return 0


async def cmd_exec(io, args):
    s = await open_session(io, args)
    try:
        return await exec_code(s, args.code.encode())
    finally:
        s.close()


async def cmd_run(io, args):
    with open(args.file, "rb") as f:
        code = f.read()
    s = await open_session(io, args)
    try:
        return await exec_code(s, code)
    finally:
        s.close()


def _human(n):
    return "%d B" % n if n < 1024 else "%.1f kB" % (n / 1024)


async def cmd_files(io, args):
    s = await open_session(io, args)
    f = s.files
    try:
        if args.cmd == "ls":
            items = sorted(await f.ls(args.path), key=lambda i: (i[1] != "d", i[0]))
            for name, kind, size in items:
                print(("%10s  %s/" % ("", name)) if kind == "d" else ("%10s  %s" % (_human(size), name)))
        elif args.cmd == "cat":
            sys.stdout.buffer.write(await f.get(args.path))
            sys.stdout.flush()
        elif args.cmd == "get":
            data = await f.get(args.path, timeout=300)
            local = args.local or os.path.basename(args.path)
            with open(local, "wb") as out:
                out.write(data)
            print("%s -> %s (%s, SHA-256 checked)" % (args.path, local, _human(len(data))), file=sys.stderr)
        elif args.cmd == "put":
            with open(args.local, "rb") as src:
                data = src.read()
            remote = args.path or "/" + os.path.basename(args.local)
            t0 = time.monotonic()
            res = await f.put(remote, data, timeout=300)
            print("%s -> %s (%s, %.1f s, SHA-256 checked)" % (args.local, remote, _human(res["size"]),
                                                              time.monotonic() - t0), file=sys.stderr)
        elif args.cmd == "rm":
            await f.rm(args.path)
        elif args.cmd == "mkdir":
            await f.mkdir(args.path)
        elif args.cmd == "rmdir":
            await f.rmdir(args.path)
        elif args.cmd == "mv":
            await f.rename(args.path, args.to)
        return 0
    except FsError as e:
        raise CliError(str(e)) from None
    finally:
        s.close()


async def cmd_info(io, args):
    s = await open_session(io, args)
    try:
        print("board    %s" % s.board)
        print("session  stage %s (%s)" % (s.stage, "one-time X25519 key" if s.stage == 2 else "REPL key only"))
        for k, v in sorted(s.info.items()):
            print("%-8s %s" % (k, v))
        return await exec_code(s, b"import iosignal.board as b\nprint('link    ', b.status())")
    finally:
        s.close()


# On the board: an empty folder for the new package, then the swap (old package kept as /lib/iosignal_old).
_RMTREE = """
import os
def _rm(p):
    try:
        names = os.listdir(p)
    except OSError:
        try:
            os.remove(p)
        except OSError:
            pass
        return
    for n in names:
        _rm(p + '/' + n)
    os.rmdir(p)
"""
_PREPARE = _RMTREE + "_rm('/lib/iosignal_new')\nos.mkdir('/lib/iosignal_new')\nprint('ready')"
_SWAP = _RMTREE + """_rm('/lib/iosignal_old')
os.rename('/lib/iosignal', '/lib/iosignal_old')
os.rename('/lib/iosignal_new', '/lib/iosignal')
print('swapped')"""


def _pkg_dir(given):
    if given:
        return given
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.normpath(os.path.join(here, "..", "..", "web", "pkg"))  # this repository (pip install -e)


async def cmd_update(io, args):
    """Puts the package of web/pkg on the board through the link (files SHA-256 checked), swaps it in whole
    and restarts. A transfer that breaks off leaves the running package as it was."""
    import json
    pkg = _pkg_dir(args.pkg)
    try:
        with open(os.path.join(pkg, "manifest.json")) as f:
            manifest = json.load(f)
    except OSError:
        raise CliError("no package at %s (run python3 tools/build_web_pkg.py, or give --pkg DIR)" % pkg) from None
    s = await open_session(io, args)
    try:
        try:
            current = json.loads(await s.files.get("/lib/iosignal/pkg.json"))
        except (FsError, ValueError):
            current = {}
        if current.get("id") == manifest["id"] and not args.force:
            print("%s already has package %s" % (s.board, manifest["id"]))
            return 0
        print("%s: package %s -> %s (%d files)" % (s.board, current.get("id", "?"), manifest["id"],
                                                  len(manifest["files"])), file=sys.stderr)
        r = RawRepl(s)
        await r.enter()
        out, err = await r.exec_raw(b"import sys\nprint(getattr(sys.implementation, '_mpy', 0) & 0xff)")
        if err or int(out.strip() or 0) != manifest["mpy"]:
            raise CliError("the board's MicroPython reads .mpy v%s, the package is v%s" % (out.strip(), manifest["mpy"]))
        out, err = await r.exec_raw(_PREPARE.encode())
        if err:
            raise CliError("could not make /lib/iosignal_new: " + err.decode(errors="replace").strip())
        await r.exit()
        t0 = time.monotonic()
        for f in manifest["files"]:
            with open(os.path.join(pkg, f["name"]), "rb") as src:
                data = src.read()
            import hashlib
            if hashlib.sha256(data).hexdigest() != f["sha256"]:
                raise CliError("%s differs from its manifest" % f["name"])
            await s.files.put("/lib/iosignal_new/" + f["name"], data, timeout=120)
        mark = json.dumps({"id": manifest["id"], "built": manifest["built"]}).encode()
        await s.files.put("/lib/iosignal_new/pkg.json", mark)
        r = RawRepl(s)
        await r.enter()
        out, err = await r.exec_raw(_SWAP.encode())
        await r.exit()
        if err or b"swapped" not in out:
            raise CliError("swap failed: " + err.decode(errors="replace").strip())
        print("%s: %d files in %.1f s, swapped in (old package in /lib/iosignal_old)" % (
            s.board, len(manifest["files"]) + 1, time.monotonic() - t0), file=sys.stderr)
        if args.no_reset:
            return 0
        s.send(b"\x03")
        await asyncio.sleep(0.2)
        s.send(b"import machine; machine.reset()\r")
        await asyncio.sleep(0.5)
        print("restarting %s; it is back in about 15 s (an anonymous board gets a new CID)" % s.board, file=sys.stderr)
        return 0
    except FsError as e:
        raise CliError(str(e)) from None
    finally:
        s.close()


async def cmd_reset(io, args):
    s = await open_session(io, args)
    s.send(b"\x03")
    await asyncio.sleep(0.2)
    s.send(b"import machine; machine.reset()\r")
    await asyncio.sleep(0.5)
    print("reset sent to %s; it is back in about 15 s" % s.board, file=sys.stderr)
    return 0


COMMANDS = {"find": cmd_find, "repl": cmd_repl, "exec": cmd_exec, "run": cmd_run, "info": cmd_info, "reset": cmd_reset,
            "update": cmd_update}
for _name in ("ls", "cat", "get", "put", "rm", "mkdir", "rmdir", "mv"):
    COMMANDS[_name] = cmd_files


def parser():
    p = argparse.ArgumentParser(prog="iosignal-repl", description=__doc__.splitlines()[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument("-s", "--server", default=os.environ.get("IOSIGNAL_SERVER", DEFAULT_SERVER))
    p.add_argument("-k", "--key", default=None, help="REPL key file (hex)")
    p.add_argument("-b", "--board", default=os.environ.get("IOSIGNAL_BOARD"), help="board CID")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("find")
    sub.add_parser("repl")
    sub.add_parser("info")
    sub.add_parser("reset")
    up = sub.add_parser("update")
    up.add_argument("--pkg", help="package folder with manifest.json (default: web/pkg of this repository)")
    up.add_argument("--force", action="store_true", help="also when the board has this package already")
    up.add_argument("--no-reset", action="store_true", help="swap it in but do not restart")
    sub.add_parser("exec").add_argument("code")
    sub.add_parser("run").add_argument("file")
    sub.add_parser("ls").add_argument("path", nargs="?", default="/")
    sub.add_parser("cat").add_argument("path")
    g = sub.add_parser("get")
    g.add_argument("path")
    g.add_argument("local", nargs="?")
    u = sub.add_parser("put")
    u.add_argument("local")
    u.add_argument("path", nargs="?")
    for name in ("rm", "mkdir", "rmdir"):
        sub.add_parser(name).add_argument("path")
    m = sub.add_parser("mv")
    m.add_argument("path")
    m.add_argument("to")
    return p


async def amain(argv=None) -> int:
    args = parser().parse_args(argv)
    io = await connect(args)
    try:
        return await COMMANDS[args.cmd](io, args)
    finally:
        await asyncio.sleep(0.2)  # let the close frame go out
        await io.aclose()


def main(argv=None) -> int:
    import logging
    logging.getLogger("iosignal").setLevel(logging.CRITICAL)
    try:
        return asyncio.run(amain(argv))
    except CliError as e:
        print("iosignal-repl: %s" % e, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:  # e.g. piped into head: stop quietly
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        return 0


if __name__ == "__main__":
    sys.exit(main())
