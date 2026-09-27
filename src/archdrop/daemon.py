"""archdropd: the Quick Share service.

The daemon owns the Quick Share receiver and browser and keeps the list of
peers and transfers.
Everything that wants to look at or steer it (the CLI, the Waybar module, the
impasto island) talks to it over a Unix socket, one JSON object per line:

    -> {"cmd": "status"}                    <- {"ok": true, "state": {...}}
    -> {"cmd": "watch", "peers": true}      <- the state, then again on every change

The state object is the one contract between the daemon and every bar.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from pathlib import Path
from typing import Any

from . import __version__, config, model, notify, quickshare

log = logging.getLogger("archdrop")

MAX_INBOUND = 4
KEEP_FINISHED = 20


class Daemon:
    def __init__(self, settings: config.Settings) -> None:
        self.settings = settings
        self.transfers: dict[str, model.Transfer] = {}
        self.peers: dict[str, model.Peer] = {}

        self._endpoint_id = quickshare.new_endpoint_id()
        self._server: asyncio.AbstractServer | None = None
        self._port = 0
        self._advertiser: quickshare.Advertiser | None = None
        self._zc = None
        self._browser: quickshare.Browser | None = None
        self._discovery_refs = 0
        self._until: float | None = None
        self._expiry: asyncio.TimerHandle | None = None
        self._inbound: set[asyncio.Task] = set()
        self._tasks: dict[str, asyncio.Task] = {}
        self._decisions: dict[str, asyncio.Future] = {}
        self._watchers: set[asyncio.Queue] = set()
        self._dirty = False
        self._stop = asyncio.Event()
        self.qs_error = ""

    # ── state ──────────────────────────────────────────────────────────────

    @property
    def qs_receiving(self) -> bool:
        return self._server is not None

    def snapshot(self) -> dict[str, Any]:
        transfers = sorted(self.transfers.values(), key=lambda t: t.created, reverse=True)
        backends = {
            "quickshare": {"available": True, "receiving": self.qs_receiving,
                           "note": self.qs_error},
        }
        return {
            "version": 1,
            "daemon": __version__,
            "name": self.settings.device_name,
            "download_dir": str(self.settings.download_path),
            "receiving": self.qs_receiving,
            "until": self._until,
            "auto_accept": self.settings.auto_accept,
            "backends": backends,
            "peers": [p.to_dict() for p in sorted(self._all_peers().values(),
                                                  key=lambda p: (p.backend, p.name.lower()))],
            "transfers": [t.to_dict() for t in transfers],
            "pending": sum(1 for t in transfers if t.state == model.PENDING),
            "active": sum(1 for t in transfers if t.state in (model.ACTIVE, model.CONNECTING,
                                                              model.WAITING)),
        }

    def _all_peers(self) -> dict[str, model.Peer]:
        return dict(self.peers)

    def changed(self) -> None:
        """Tell every watcher, at most ten times a second."""
        if self._dirty:
            return
        self._dirty = True
        asyncio.get_running_loop().call_later(0.1, self._flush)

    def _flush(self) -> None:
        self._dirty = False
        state = self.snapshot()
        for queue in list(self._watchers):
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(state)

    def _trim(self) -> None:
        finished = sorted((t for t in self.transfers.values() if t.finished),
                          key=lambda t: t.updated)
        for old in finished[:-KEEP_FINISHED]:
            del self.transfers[old.id]

    # ── receiving ──────────────────────────────────────────────────────────

    async def _zeroconf(self):
        if self._zc is None:
            from zeroconf import IPVersion
            from zeroconf.asyncio import AsyncZeroconf
            addresses = quickshare.local_ipv4_addresses()
            kwargs: dict[str, Any] = {"ip_version": IPVersion.V4Only}
            if addresses:
                kwargs["interfaces"] = addresses
            self._zc = AsyncZeroconf(**kwargs)
        return self._zc

    async def start_receiving(self, minutes: int | None) -> list[str]:
        """Become visible. Returns one message per backend that could not."""
        problems: list[str] = []
        if self._server is None:
            try:
                self._server = await asyncio.start_server(
                    self._on_connection, "0.0.0.0", self.settings.port)
                self._port = self._server.sockets[0].getsockname()[1]
                self._advertiser = quickshare.Advertiser(
                    self.settings.device_name, self._port, self._endpoint_id)
                await self._advertiser.start()
                self.qs_error = ""
            except Exception as error:
                log.exception("cannot start Quick Share receiver")
                self.qs_error = f"Quick Share: {error}"
                problems.append(self.qs_error)
                await self._stop_quickshare()
        self._set_expiry(minutes)
        if self.qs_receiving:
            log.info("visible as %r on port %d%s", self.settings.device_name, self._port,
                     f" for {minutes} min" if minutes else " until switched off")
        self.changed()
        return problems

    async def stop_receiving(self) -> None:
        if self.qs_receiving:
            log.info("no longer visible")
        self._set_expiry(None, clear=True)
        await self._stop_quickshare()
        self.changed()

    async def _stop_quickshare(self) -> None:
        if self._advertiser:
            try:
                await self._advertiser.stop()
            except Exception:
                log.exception("unregistering the mDNS service")
            self._advertiser = None
        if self._server:
            self._server.close()
            self._server = None

    def _set_expiry(self, minutes: int | None, clear: bool = False) -> None:
        if self._expiry:
            self._expiry.cancel()
            self._expiry = None
        self._until = None
        if clear or not minutes:
            return
        self._until = time.time() + minutes * 60
        loop = asyncio.get_running_loop()
        self._expiry = loop.call_later(minutes * 60, lambda: asyncio.ensure_future(
            self.stop_receiving()))

    async def _on_connection(self, reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        log.info("connection from %s", peer[0] if peer else "?")
        if len(self._inbound) >= MAX_INBOUND:
            writer.close()
            return
        session = quickshare.InboundSession(
            reader, writer, download_dir=self.settings.download_path,
            update=self._on_update, decide=self._decide, on_text=self._on_text)
        task = asyncio.current_task()
        assert task is not None
        self._inbound.add(task)
        try:
            await session.run()
        finally:
            self._inbound.discard(task)

    async def _decide(self, transfer: model.Transfer) -> bool:
        if self.settings.auto_accept:
            return True
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._decisions[transfer.id] = future
        if self.settings.notify:
            what = (transfer.text[:60] if transfer.kind != "files" else
                    ", ".join(f["name"] for f in transfer.files[:3]) +
                    ("…" if len(transfer.files) > 3 else ""))
            await notify.notify(f"{transfer.peer} wants to share",
                                f"{what}\nCode {transfer.pin}. Accept from the Archdrop panel "
                                f"or run: archdrop accept", urgent=True)
        try:
            return await future
        finally:
            self._decisions.pop(transfer.id, None)

    def _on_text(self, transfer: model.Transfer, text: str) -> None:
        asyncio.ensure_future(notify.copy(text))
        if self.settings.notify:
            asyncio.ensure_future(notify.notify(
                f"{transfer.peer} sent a {'link' if transfer.kind == 'url' else 'message'}",
                text[:200] + "\n(copied to the clipboard)"))

    def _on_update(self, transfer: model.Transfer) -> None:
        previous = self.transfers.get(transfer.id)
        self.transfers[transfer.id] = transfer
        if (previous is None or previous.state != transfer.state) and self.settings.notify:
            asyncio.ensure_future(self._announce(transfer))
        if transfer.finished:
            self._trim()
        self.changed()

    async def _announce(self, t: model.Transfer) -> None:
        if t.direction == "in" and t.state == model.DONE and t.kind == "files":
            await notify.notify(f"Received from {t.peer}",
                                "\n".join(Path(p).name for p in t.saved[:5]))
        elif t.direction == "out" and t.state == model.DONE:
            await notify.notify(f"Sent to {t.peer}", ", ".join(f["name"] for f in t.files[:5]) or t.kind)
        elif t.state == model.FAILED:
            await notify.notify("Transfer failed", f"{t.peer}: {t.error}", urgent=True)
        elif t.state == model.REJECTED and t.direction == "out":
            await notify.notify(f"{t.peer} declined", t.error)

    # ── decisions ──────────────────────────────────────────────────────────

    def decide(self, transfer_id: str, accept: bool) -> str:
        future = self._decisions.get(transfer_id)
        if future is None or future.done():
            return "nothing is waiting for an answer with that id"
        future.set_result(accept)
        return ""

    def pending_ids(self) -> list[str]:
        return [i for i, f in self._decisions.items() if not f.done()]

    def cancel(self, transfer_id: str) -> str:
        task = self._tasks.get(transfer_id)
        if task and not task.done():
            task.cancel()
            return ""
        return self.decide(transfer_id, False) if transfer_id in self._decisions \
            else "no such transfer in progress"

    # ── discovery ──────────────────────────────────────────────────────────

    async def use_discovery(self, on: bool) -> None:
        self._discovery_refs = max(0, self._discovery_refs + (1 if on else -1))
        if on and self._browser is None:
            try:
                zc = await self._zeroconf()
                self._browser = quickshare.Browser(zc, self._peers_changed, self._endpoint_id)
                await self._browser.start()
            except Exception:
                log.exception("cannot browse for devices")
                self._browser = None
        elif not on and self._discovery_refs == 0 and self._browser is not None:
            await self._browser.stop()
            self._browser = None
            self.peers = {}
            self.changed()

    def _peers_changed(self, peers: dict[str, model.Peer]) -> None:
        self.peers = dict(peers)
        self.changed()

    async def scan(self, seconds: float) -> list[model.Peer]:
        await self.use_discovery(True)
        try:
            await asyncio.sleep(seconds)
        finally:
            await self.use_discovery(False)
        return list(self._all_peers().values())

    def find_peer(self, ref: str) -> model.Peer | str:
        peers = self._all_peers()
        if ref in peers:
            return peers[ref]
        matches = [p for p in peers.values() if ref.lower() in p.name.lower()]
        exact = [p for p in matches if p.name.lower() == ref.lower()]
        matches = exact or matches
        if len(matches) == 1:
            return matches[0]
        if not matches:
            return f"no device matching {ref!r} is visible"
        return f"{ref!r} matches several devices: " + ", ".join(p.name for p in matches)

    # ── sending ────────────────────────────────────────────────────────────

    async def send(self, ref: str, paths: list[str], text: str | None) -> tuple[str, str]:
        """Start a send. Returns ``(transfer_id, error)``."""
        files: list[Path] = []
        if text is None:
            if not paths:
                return "", "nothing to send"
            for raw in paths:
                path = Path(raw)
                if not path.is_absolute() or not path.is_file():
                    return "", f"{raw}: not a file (folders must be archived first)"
                if not os.access(path, os.R_OK):
                    return "", f"{raw}: cannot be read"
                files.append(path)
        if not self._all_peers() or isinstance(self.find_peer(ref), str):
            await self.scan(3.5)
        peer = self.find_peer(ref)
        if isinstance(peer, str):
            return "", peer
        transfer = model.Transfer(peer.backend, "out", peer.name)
        self.transfers[transfer.id] = transfer
        coro = quickshare.send(peer.address, peer.port, device_name=self.settings.device_name,
                               peer_name=peer.name, paths=files, text=text,
                               update=self._on_update, transfer=transfer)
        task = asyncio.ensure_future(self._guard(coro, transfer))
        self._tasks[transfer.id] = task
        task.add_done_callback(lambda _t, i=transfer.id: self._tasks.pop(i, None))
        self.changed()
        return transfer.id, ""

    async def _guard(self, coro, transfer: model.Transfer) -> None:
        try:
            await coro
        except asyncio.CancelledError:
            pass
        except Exception as error:
            log.exception("send failed")
            transfer.state, transfer.error = model.FAILED, str(error)
            self._on_update(transfer)

    # ── requests ───────────────────────────────────────────────────────────

    async def handle(self, request: dict, writer: asyncio.StreamWriter) -> dict | None:
        cmd = request.get("cmd")
        if cmd == "status":
            return {"ok": True, "state": self.snapshot()}
        if cmd == "on":
            minutes = request.get("minutes", self.settings.default_minutes)
            problems = await self.start_receiving(minutes if minutes else None)
            return {"ok": not problems or self.qs_receiving, "error": "; ".join(problems),
                    "state": self.snapshot()}
        if cmd == "off":
            await self.stop_receiving()
            return {"ok": True, "state": self.snapshot()}
        if cmd == "toggle":
            if self.snapshot()["receiving"]:
                await self.stop_receiving()
            else:
                await self.start_receiving(self.settings.default_minutes or None)
            return {"ok": True, "state": self.snapshot()}
        if cmd == "peers":
            peers = await self.scan(float(request.get("seconds", 4)))
            return {"ok": True, "peers": [p.to_dict() for p in peers]}
        if cmd == "send":
            transfer_id, error = await self.send(
                str(request.get("peer", "")), list(request.get("paths", [])), request.get("text"))
            return {"ok": not error, "error": error, "id": transfer_id}
        if cmd in ("accept", "reject"):
            target = request.get("id") or (self.pending_ids() or [""])[0]
            error = self.decide(target, cmd == "accept")
            return {"ok": not error, "error": error}
        if cmd == "cancel":
            error = self.cancel(str(request.get("id", "")))
            return {"ok": not error, "error": error}
        if cmd == "clear":
            for tid in [i for i, t in self.transfers.items() if t.finished]:
                del self.transfers[tid]
            self.changed()
            return {"ok": True}
        if cmd == "set":
            key, value = request.get("key"), request.get("value")
            expected = config.EDITABLE.get(key)
            if expected is None or not isinstance(value, expected):
                return {"ok": False, "error": f"cannot set {key!r}"}
            setattr(self.settings, key, value)
            config.save_override(key, value)
            self.changed()
            return {"ok": True, "state": self.snapshot()}
        if cmd == "watch":
            return await self._watch(request, writer)
        return {"ok": False, "error": f"unknown command {cmd!r}"}

    async def _watch(self, request: dict, writer: asyncio.StreamWriter) -> None:
        wants_peers = bool(request.get("peers"))
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        self._watchers.add(queue)
        if wants_peers:
            await self.use_discovery(True)
        try:
            queue.put_nowait(self.snapshot())
            while True:
                state = await queue.get()
                writer.write((json.dumps({"event": "state", "state": state}) + "\n").encode())
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            self._watchers.discard(queue)
            if wants_peers:
                await self.use_discovery(False)
        return None

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while line := await reader.readline():
                try:
                    request = json.loads(line)
                    if not isinstance(request, dict):
                        raise ValueError
                except ValueError:
                    response: dict | None = {"ok": False, "error": "bad request"}
                else:
                    response = await self.handle(request, writer)
                if response is None:
                    return
                writer.write((json.dumps(response) + "\n").encode())
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()

    # ── lifecycle ──────────────────────────────────────────────────────────

    async def run(self) -> None:
        path = config.socket_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            # A live daemon answers; a stale socket does not.
            try:
                _, w = await asyncio.open_unix_connection(str(path))
                w.close()
                raise SystemExit("archdropd is already running")
            except (ConnectionError, FileNotFoundError):
                path.unlink(missing_ok=True)
        old_umask = os.umask(0o177)
        try:
            server = await asyncio.start_unix_server(self._client, str(path))
        finally:
            os.umask(old_umask)
        os.chmod(path, 0o600)

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._stop.set)

        if self.settings.receive_on_start:
            await self.start_receiving(None)
        log.info("archdropd %s listening on %s", __version__, path)
        await self._stop.wait()

        for task in list(self._tasks.values()) + list(self._inbound):
            task.cancel()
        server.close()
        await self.stop_receiving()
        if self._browser:
            await self._browser.stop()
        if self._zc:
            await self._zc.async_close()
        path.unlink(missing_ok=True)


def main() -> None:
    logging.basicConfig(level=os.environ.get("ARCHDROP_LOG", "INFO"),
                        format="%(levelname)s %(name)s: %(message)s")
    asyncio.run(Daemon(config.load()).run())
