"""Our sender against our receiver, over a real socket."""

import asyncio
import os
from pathlib import Path

from archdrop import model, quickshare


async def _serve(tmp_path, decide, updates, texts):
    async def handle(reader, writer):
        session = quickshare.InboundSession(
            reader, writer, download_dir=tmp_path / "in",
            update=lambda t: updates.append((t.state, t.done)), decide=decide,
            on_text=lambda t, text: texts.append(text))
        await session.run()
    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


def test_files_roundtrip(tmp_path):
    big = tmp_path / "big.bin"
    big.write_bytes(os.urandom(1_300_000))
    small = tmp_path / "note.txt"
    small.write_text("hello")
    empty = tmp_path / "empty.dat"
    empty.write_bytes(b"")

    async def main():
        updates, texts = [], []

        async def accept(t):
            assert t.files and t.pin
            return True
        server, port = await _serve(tmp_path, accept, updates, texts)
        seen = []
        result = await quickshare.send("127.0.0.1", port, device_name="sender", peer_name="rx",
                                       paths=[big, small, empty], text=None,
                                       update=lambda t: seen.append(t.state))
        await asyncio.sleep(0.3)
        server.close()
        return result, updates, seen

    result, updates, seen = asyncio.run(main())
    assert result.state == model.DONE, result.error
    assert (tmp_path / "in" / "big.bin").read_bytes() == big.read_bytes()
    assert (tmp_path / "in" / "note.txt").read_text() == "hello"
    assert (tmp_path / "in" / "empty.dat").exists()
    assert not list((tmp_path / "in").glob("*.part"))
    assert updates[-1][0] == model.DONE
    assert "waiting" in seen and "active" in seen


def test_reject(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("x")

    async def main():
        updates = []

        async def no(t):
            return False
        server, port = await _serve(tmp_path, no, updates, [])
        result = await quickshare.send("127.0.0.1", port, device_name="s", peer_name="r",
                                       paths=[f], text=None, update=lambda t: None)
        await asyncio.sleep(0.2)
        server.close()
        return result, updates

    result, updates = asyncio.run(main())
    assert result.state == model.REJECTED
    assert not (tmp_path / "in" / "a.txt").exists()
    assert updates[-1][0] == model.REJECTED


def test_text_and_url(tmp_path):
    async def main():
        texts = []

        async def yes(t):
            return True
        server, port = await _serve(tmp_path, yes, [], texts)
        for value in ("just words", "https://example.com/x?y=1"):
            r = await quickshare.send("127.0.0.1", port, device_name="s", peer_name="r",
                                      paths=[], text=value, update=lambda t: None)
            assert r.state == model.DONE, r.error
        await asyncio.sleep(0.2)
        server.close()
        return texts

    assert asyncio.run(main()) == ["just words", "https://example.com/x?y=1"]


def test_pins_match(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("x")

    async def main():
        pins = []

        async def yes(t):
            pins.append(t.pin)
            return True
        server, port = await _serve(tmp_path, yes, [], [])
        r = await quickshare.send("127.0.0.1", port, device_name="s", peer_name="r",
                                  paths=[f], text=None, update=lambda t: None)
        server.close()
        return pins, r

    pins, r = asyncio.run(main())
    assert pins[0] == r.pin and len(r.pin) == 4


def test_hostile_names():
    assert quickshare.safe_name("../../etc/passwd") == "passwd"
    assert quickshare.safe_name("..\\..\\x.exe") == "x.exe"
    assert quickshare.safe_name("...") == "file"
    assert quickshare.safe_name(".bashrc") == "bashrc"
    assert quickshare.safe_name("") == "file"


def test_hostile_clients_do_not_hurt_the_receiver(tmp_path):
    """Garbage, an oversize length, and an early hang-up each end one session
    and leave the listener serving the next honest sender."""
    f = tmp_path / "ok.txt"
    f.write_text("fine")

    async def main():
        updates = []

        async def yes(t):
            return True
        server, port = await _serve(tmp_path, yes, updates, [])
        for payload in (b"GET / HTTP/1.1\r\n\r\n", b"\xff\xff\xff\xff" + b"x" * 64,
                        b"\x00\x00\x00\x05hello", b""):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(payload)
            await writer.drain()
            writer.close()
            await asyncio.sleep(0.1)
        result = await quickshare.send("127.0.0.1", port, device_name="s", peer_name="r",
                                       paths=[f], text=None, update=lambda t: None)
        server.close()
        return result

    result = asyncio.run(main())
    assert result.state == model.DONE, result.error
    assert (tmp_path / "in" / "ok.txt").read_text() == "fine"
