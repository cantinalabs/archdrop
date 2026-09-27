"""The daemon's request handling, with peers injected instead of discovered."""

import asyncio

from archdrop import config, daemon, model


def make(tmp_path, **overrides):
    settings = config.Settings(download_dir=str(tmp_path / "dl"), notify=False, **overrides)
    return daemon.Daemon(settings)


async def open_receiver(d):
    server = await asyncio.start_server(d._on_connection, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def until(predicate, tries=100):
    for _ in range(tries):
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition never became true")


def phone(port):
    return model.Peer("qs:x", "quickshare", "Pixel", "phone", "127.0.0.1", port)


def test_send_to_a_peer_and_consent(tmp_path):
    f = tmp_path / "pic.jpg"
    f.write_bytes(b"\xff\xd8" + bytes(50_000))

    async def main():
        rx, tx = make(tmp_path), make(tmp_path)
        server, port = await open_receiver(rx)
        tx.peers["qs:x"] = phone(port)
        reply = await tx.handle({"cmd": "send", "peer": "pix", "paths": [str(f)]}, None)
        assert reply["ok"], reply
        await until(rx.pending_ids)
        assert rx.snapshot()["pending"] == 1
        assert (await rx.handle({"cmd": "accept"}, None))["ok"]
        await until(lambda: tx.transfers[reply["id"]].finished)
        server.close()
        return tx.transfers[reply["id"]], rx.snapshot()

    sent, state = asyncio.run(main())
    assert sent.state == model.DONE
    assert (tmp_path / "dl" / "pic.jpg").read_bytes() == f.read_bytes()
    assert state["transfers"][0]["state"] == "done"


def test_reject_and_bad_requests(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("a")

    async def main():
        rx, tx = make(tmp_path), make(tmp_path)
        server, port = await open_receiver(rx)
        tx.peers["qs:x"] = phone(port)
        missing = await tx.handle({"cmd": "send", "peer": "Pixel", "paths": ["/nonexistent"]}, None)
        relative = await tx.handle({"cmd": "send", "peer": "Pixel", "paths": ["a.txt"]}, None)
        reply = await tx.handle({"cmd": "send", "peer": "Pixel", "paths": [str(f)]}, None)
        await until(rx.pending_ids)
        await rx.handle({"cmd": "reject"}, None)
        await until(lambda: tx.transfers[reply["id"]].finished)
        server.close()
        return missing, relative, tx.transfers[reply["id"]]

    missing, relative, result = asyncio.run(main())
    assert not missing["ok"] and not relative["ok"]
    assert result.state == model.REJECTED
    assert not (tmp_path / "dl" / "a.txt").exists()


def test_auto_accept(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("auto")

    async def main():
        rx, tx = make(tmp_path, auto_accept=True), make(tmp_path)
        server, port = await open_receiver(rx)
        tx.peers["qs:x"] = phone(port)
        reply = await tx.handle({"cmd": "send", "peer": "Pixel", "paths": [str(f)]}, None)
        await until(lambda: tx.transfers[reply["id"]].finished)
        server.close()
        return tx.transfers[reply["id"]]

    assert asyncio.run(main()).state == model.DONE
    assert (tmp_path / "dl" / "a.txt").read_text() == "auto"


def test_cancel_while_waiting_for_the_other_side(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("a")

    async def main():
        rx, tx = make(tmp_path), make(tmp_path)
        server, port = await open_receiver(rx)
        tx.peers["qs:x"] = phone(port)
        reply = await tx.handle({"cmd": "send", "peer": "Pixel", "paths": [str(f)]}, None)
        await until(rx.pending_ids)
        cancelled = await tx.handle({"cmd": "cancel", "id": reply["id"]}, None)
        await asyncio.sleep(0.3)
        server.close()
        return cancelled, tx.transfers[reply["id"]]

    cancelled, transfer = asyncio.run(main())
    assert cancelled["ok"]
    assert transfer.state == model.CANCELLED


def test_settings_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")

    async def main():
        d = make(tmp_path)
        assert (await d.handle({"cmd": "set", "key": "name", "value": "Study"}, None))["ok"]
        assert not (await d.handle({"cmd": "set", "key": "port", "value": 1}, None))["ok"]
        assert not (await d.handle({"cmd": "set", "key": "name", "value": 5}, None))["ok"]
        return d.settings.device_name

    assert asyncio.run(main()) == "Study"
    assert config.load().name == "Study"


def test_visible_by_default_and_until_switched_off():
    s = config.Settings()
    assert s.receive_on_start and s.default_minutes == 0
