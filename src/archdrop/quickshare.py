"""Android Quick Share (Nearby Share) over the local network.

Two people on the same network find each other through mDNS: a device that is
willing to receive advertises the service ``_FC9F5ED42C8A._tcp`` whose instance
name encodes a four-character endpoint id and whose ``n`` text record carries
its display name. Whoever wants to send connects to the advertised TCP port and
runs the protocol in :mod:`archdrop.channel`.

This module is the two ends of a transfer (:class:`InboundSession`,
:func:`send`) and the mDNS half (:class:`Advertiser`, :class:`Browser`).
Bluetooth wake-up and Wi-Fi Direct are not implemented, so both devices must be
on the same network, and the phone must be visible to "Everyone" while it is
receiving.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
import os
import random
import re
import shutil
import socket
import string
import time
from pathlib import Path
from typing import Awaitable, Callable

from . import mdns, messages as m
from . import model, pb, secure
from .channel import (CHUNK_SIZE, Channel, BytesPayload, Disconnected, FileChunk,
                      ProtocolError, client_handshake, endpoint_info, new_payload_id,
                      parse_endpoint_info, server_handshake, share_frame)

log = logging.getLogger("archdrop.quickshare")

SERVICE_TYPE = "_FC9F5ED42C8A._tcp.local."
HANDSHAKE_TIMEOUT = 20
INTRODUCTION_TIMEOUT = 30
CONSENT_TIMEOUT = 90
IDLE_TIMEOUT = 60
MAX_FILES = 500
DEVICE_KINDS = {0: "unknown", 1: "phone", 2: "tablet", 3: "laptop"}

Update = Callable[[model.Transfer], None]


def new_endpoint_id() -> str:
    return "".join(random.SystemRandom().choices(string.ascii_letters + string.digits, k=4))


def mdns_instance_name(endpoint_id: str) -> str:
    """Instance name: 0x23, the id, the service hash FC 9F 5E, two zero bytes."""
    raw = b"\x23" + endpoint_id.encode("ascii") + b"\xfc\x9f\x5e\x00\x00"
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def endpoint_id_from_instance(name: str) -> str | None:
    try:
        raw = base64.urlsafe_b64decode(name + "=" * (-len(name) % 4))
    except ValueError:
        return None
    if len(raw) == 10 and raw[0] == 0x23:
        return raw[1:5].decode("ascii", "replace")
    return None


def safe_name(name: str) -> str:
    """A file name that cannot leave the download folder or hide."""
    name = name.replace("\\", "/").split("/")[-1]
    name = re.sub(r"[\x00-\x1f]", "", name).strip()
    name = name.lstrip(".") or "file"
    encoded = name.encode("utf-8")[:200]
    return encoded.decode("utf-8", "ignore") or "file"


def unique_path(directory: Path, name: str) -> Path:
    candidate = directory / name
    stem, suffix = os.path.splitext(name)
    counter = 1
    while candidate.exists() or Path(f"{candidate}.part").exists():
        candidate = directory / f"{stem} ({counter}){suffix}"
        counter += 1
    return candidate


# ── receiving ──────────────────────────────────────────────────────────────

class InboundSession:
    """One incoming connection, from handshake to the last byte.

    ``decide`` is asked once the sender has said what it wants to send; it
    returns whether to accept. It runs beside the read loop so keep-alives are
    still answered while a person thinks.
    """

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, *,
                 download_dir: Path, update: Update,
                 decide: Callable[[model.Transfer], Awaitable[bool]],
                 on_text: Callable[[model.Transfer, str], None]) -> None:
        self.chan = Channel(reader, writer)
        peername = writer.get_extra_info("peername") or ("?", 0)
        self.host = peername[0]
        self.download_dir = download_dir
        self.update = update
        self.decide = decide
        self.on_text = on_text
        self.transfer = model.Transfer("quickshare", "in", self.host)
        self._files: dict[int, dict] = {}
        self._text_id: int | None = None
        self._consent: asyncio.Task | None = None
        self._introduced = False

    def _set(self, state: str | None = None, error: str = "") -> None:
        if state:
            self.transfer.state = state
        if error:
            self.transfer.error = error
        self.transfer.updated = time.time()
        self.update(self.transfer)

    async def run(self) -> None:
        chan = self.chan
        try:
            try:
                keys, (name, kind) = await asyncio.wait_for(server_handshake(chan), HANDSHAKE_TIMEOUT)
            except asyncio.TimeoutError as error:
                raise ProtocolError("handshake timed out") from error
            self.transfer.peer = name
            self.transfer.pin = keys.pin
            self._set(model.CONNECTING)
            chan.start_keepalive()
            await chan.send_share(share_frame(
                m.SHARE_PAIRED_KEY_ENCRYPTION,
                paired_key_encryption={"secret_id_hash": os.urandom(6),
                                       "signed_data": os.urandom(72)}))
            deadline = time.monotonic() + INTRODUCTION_TIMEOUT
            while True:
                timeout = IDLE_TIMEOUT if self._introduced else max(1, deadline - time.monotonic())
                try:
                    event = await asyncio.wait_for(chan.next_event(), timeout)
                except asyncio.TimeoutError as error:
                    raise ProtocolError("timed out waiting for the sender") from error
                if isinstance(event, BytesPayload):
                    if await self._on_bytes(event):
                        break
                elif await self._on_file(event):
                    break
        except Disconnected:
            if not self.transfer.finished:
                self._set(model.FAILED, "the sender disconnected")
        except ProtocolError as error:
            log.warning("inbound from %s: %s", self.host, error)
            if not self.transfer.finished:
                self._set(model.FAILED, str(error))
        except Exception as error:  # a bug must not take the daemon down
            log.exception("inbound from %s failed", self.host)
            if not self.transfer.finished:
                self._set(model.FAILED, f"internal error: {error}")
        finally:
            if self._consent:
                self._consent.cancel()
            self._discard_partials()
            await chan.close()

    def _discard_partials(self) -> None:
        for info in self._files.values():
            if info["handle"]:
                info["handle"].close()
                info["handle"] = None
            if info["part"] and not info["complete"]:
                Path(info["part"]).unlink(missing_ok=True)

    async def _on_bytes(self, payload: BytesPayload) -> bool:
        """Returns True when the session is over."""
        if payload.payload_id == self._text_id:
            text = payload.data.decode("utf-8", "replace").rstrip("\x00")
            self.transfer.text = text
            self.transfer.done = self.transfer.total = len(payload.data)
            self._set(model.DONE)
            self.on_text(self.transfer, text)
            await self.chan.send_disconnection()
            return True
        try:
            frame = pb.decode(m.SHARE_FRAME, payload.data)
        except pb.DecodeError as error:
            raise ProtocolError(f"bad share frame: {error}") from error
        v1 = frame.get("v1") or {}
        kind = v1.get("type")
        if kind == m.SHARE_CANCEL:
            self._set(model.CANCELLED)
            await self.chan.send_disconnection()
            return True
        if kind == m.SHARE_PAIRED_KEY_ENCRYPTION:
            # No paired certificates here, so say so and carry on as "everyone".
            await self.chan.send_share(share_frame(
                m.SHARE_PAIRED_KEY_RESULT, paired_key_result={"status": m.PAIRED_UNABLE}))
        elif kind == m.SHARE_INTRODUCTION and not self._introduced:
            await self._introduce(v1.get("introduction") or {})
        return False

    async def _introduce(self, intro: dict) -> None:
        files = intro.get("file_metadata", [])
        texts = intro.get("text_metadata", [])
        if len(files) > MAX_FILES:
            await self._refuse(m.STATUS_UNSUPPORTED_ATTACHMENT, "too many files")
            return
        if intro.get("wifi_credentials_metadata") or (files and texts) or len(texts) > 1 \
                or not (files or texts):
            await self._refuse(m.STATUS_UNSUPPORTED_ATTACHMENT, "unsupported attachment")
            return
        self._introduced = True
        if texts:
            meta = texts[0]
            kind = "url" if meta.get("type") == m.TEXT_URL else "text"
            self._text_id = meta.get("payload_id")
            self.transfer.kind = kind
            self.transfer.text = meta.get("text_title", "")
            self.transfer.total = meta.get("size", 0)
        else:
            self.download_dir.mkdir(parents=True, exist_ok=True)
            total = 0
            for meta in files:
                size = meta.get("size", 0)
                if size < 0:
                    await self._refuse(m.STATUS_UNSUPPORTED_ATTACHMENT, "bad file size")
                    return
                name = safe_name(meta.get("name", "file"))
                self._files[meta.get("payload_id", 0)] = {
                    "name": name, "size": size, "received": 0, "handle": None,
                    "path": None, "part": None, "complete": False}
                self.transfer.files.append({"name": name, "size": size})
                total += size
            free = shutil.disk_usage(self.download_dir).free
            if total > free - (256 << 20):
                await self._refuse(m.STATUS_NOT_ENOUGH_SPACE, "not enough disk space")
                return
            self.transfer.total = total
        self._set(model.PENDING)
        self._consent = asyncio.create_task(self._ask())

    async def _refuse(self, status: int, reason: str) -> None:
        await self.chan.send_share(share_frame(
            m.SHARE_RESPONSE, connection_response={"status": status}))
        self._set(model.REJECTED, reason)
        await self.chan.send_disconnection()
        raise Disconnected(reason)

    async def _ask(self) -> None:
        try:
            accepted = await asyncio.wait_for(self.decide(self.transfer), CONSENT_TIMEOUT)
        except asyncio.TimeoutError:
            accepted = False
            status = m.STATUS_TIMED_OUT
        else:
            status = m.STATUS_ACCEPT if accepted else m.STATUS_REJECT
        try:
            if accepted:
                for info in self._files.values():
                    path = unique_path(self.download_dir, info["name"])
                    info["path"] = path
                    info["part"] = f"{path}.part"
                    info["handle"] = open(info["part"], "wb")
                if self._text_id is None and not self._files:
                    accepted = False
            await self.chan.send_share(share_frame(
                m.SHARE_RESPONSE, connection_response={"status": status}))
            if accepted:
                self._set(model.ACTIVE)
            else:
                self._set(model.REJECTED)
                await self.chan.send_disconnection()
                await self.chan.close()
        except (Disconnected, OSError) as error:
            self._set(model.FAILED, str(error))
            await self.chan.close()

    async def _on_file(self, chunk: FileChunk) -> bool:
        info = self._files.get(chunk.payload_id)
        if info is None or info["handle"] is None:
            raise ProtocolError("data for a file that was not accepted")
        if chunk.offset != info["received"]:
            raise ProtocolError("file chunk out of order")
        if info["received"] + len(chunk.body) > info["size"]:
            raise ProtocolError("file is larger than announced")
        if chunk.body:
            await asyncio.to_thread(info["handle"].write, chunk.body)
            info["received"] += len(chunk.body)
            self.transfer.done += len(chunk.body)
            self.transfer.updated = time.time()
            self.update(self.transfer)
        if chunk.last:
            if info["received"] != info["size"]:
                raise ProtocolError("file ended early")
            info["handle"].close()
            info["handle"] = None
            os.replace(info["part"], info["path"])
            info["complete"] = True
            self.transfer.saved.append(str(info["path"]))
        return await self._finish_files() if all(i["complete"] for i in self._files.values()) \
            else False

    async def _finish_files(self) -> bool:
        self._set(model.DONE)
        await self.chan.send_disconnection()
        return True


# ── sending ────────────────────────────────────────────────────────────────

def _file_type(mime: str, name: str) -> int:
    if mime.startswith("image/"):
        return m.FILE_IMAGE
    if mime.startswith("video/"):
        return m.FILE_VIDEO
    if mime.startswith("audio/"):
        return m.FILE_AUDIO
    if name.endswith(".apk"):
        return m.FILE_APP
    return m.FILE_UNKNOWN


async def send(host: str, port: int, *, device_name: str, peer_name: str,
               paths: list[Path], text: str | None, update: Update,
               transfer: model.Transfer | None = None) -> model.Transfer:
    """Send files, or one piece of text or a URL, to a listening device."""
    transfer = transfer or model.Transfer("quickshare", "out", peer_name)
    transfer.peer = peer_name

    def set_state(state: str, error: str = "") -> None:
        transfer.state = state
        transfer.error = error
        transfer.updated = time.time()
        update(transfer)

    set_state(model.CONNECTING)
    chan: Channel | None = None
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), 10)
        chan = Channel(reader, writer)
        endpoint_id = new_endpoint_id()
        keys = await asyncio.wait_for(client_handshake(chan, endpoint_id, device_name),
                                      HANDSHAKE_TIMEOUT)
        transfer.pin = keys.pin
        chan.start_keepalive()
        set_state(model.CONNECTING)

        files: dict[int, tuple[Path, int]] = {}
        introduction: dict = {}
        if text is not None:
            payload = text.encode("utf-8")
            is_url = bool(re.match(r"^https?://\S+$", text.strip()))
            text_id = new_payload_id()
            transfer.kind = "url" if is_url else "text"
            transfer.text = text
            transfer.total = len(payload)
            introduction["text_metadata"] = [{
                "text_title": text.strip()[:60], "type": m.TEXT_URL if is_url else m.TEXT_TEXT,
                "payload_id": text_id, "size": len(payload)}]
        else:
            metas = []
            for path in paths:
                size = path.stat().st_size
                mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                payload_id = new_payload_id()
                files[payload_id] = (path, size)
                transfer.files.append({"name": path.name, "size": size})
                transfer.total += size
                metas.append({"name": path.name, "type": _file_type(mime, path.name),
                              "payload_id": payload_id, "size": size, "mime_type": mime})
            introduction["file_metadata"] = metas

        await chan.send_share(share_frame(
            m.SHARE_PAIRED_KEY_ENCRYPTION,
            paired_key_encryption={"secret_id_hash": os.urandom(6),
                                   "signed_data": os.urandom(72)}))

        introduced = False
        set_state(model.WAITING)
        deadline = time.monotonic() + 120
        while True:
            try:
                event = await asyncio.wait_for(chan.next_event(),
                                               max(1, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                raise ProtocolError("nobody answered on the other device") from None
            if not isinstance(event, BytesPayload):
                continue
            try:
                frame = pb.decode(m.SHARE_FRAME, event.data)
            except pb.DecodeError as error:
                raise ProtocolError(f"bad share frame: {error}") from error
            v1 = frame.get("v1") or {}
            kind = v1.get("type")
            if kind == m.SHARE_PAIRED_KEY_ENCRYPTION:
                await chan.send_share(share_frame(
                    m.SHARE_PAIRED_KEY_RESULT, paired_key_result={"status": m.PAIRED_UNABLE}))
            elif kind == m.SHARE_PAIRED_KEY_RESULT and not introduced:
                introduced = True
                await chan.send_share(share_frame(m.SHARE_INTRODUCTION, introduction=introduction))
            elif kind == m.SHARE_CANCEL:
                set_state(model.CANCELLED, "cancelled on the other device")
                return transfer
            elif kind == m.SHARE_RESPONSE:
                status = (v1.get("connection_response") or {}).get("status")
                if status == m.STATUS_ACCEPT:
                    break
                reasons = {m.STATUS_REJECT: "declined",
                           m.STATUS_NOT_ENOUGH_SPACE: "not enough space on the device",
                           m.STATUS_UNSUPPORTED_ATTACHMENT: "the device cannot take this",
                           m.STATUS_TIMED_OUT: "timed out on the device"}
                set_state(model.REJECTED, reasons.get(status, "declined"))
                await chan.send_disconnection()
                return transfer

        set_state(model.ACTIVE)
        if text is not None:
            await chan.send_bytes_payload(text_id, text.encode("utf-8"))
            transfer.done = transfer.total
        else:
            for payload_id, (path, size) in files.items():
                offset = 0
                with open(path, "rb") as handle:
                    while offset < size:
                        body = await asyncio.to_thread(handle.read, CHUNK_SIZE)
                        if not body:
                            raise ProtocolError(f"{path.name} changed while sending")
                        await chan.send_file_chunk(payload_id, size, path.name, offset, body)
                        offset += len(body)
                        transfer.done += len(body)
                        transfer.updated = time.time()
                        update(transfer)
                await chan.send_file_chunk(payload_id, size, path.name, size, b"", last=True)
        # Let the peer drain what it has before the connection goes away.
        await chan.send_disconnection()
        await asyncio.sleep(0.5)
        set_state(model.DONE)
        return transfer
    except asyncio.CancelledError:
        if chan and chan.crypto:
            try:
                await chan.send_share(share_frame(m.SHARE_CANCEL))
            except Exception:
                pass
        set_state(model.CANCELLED)
        raise
    except (Disconnected, ProtocolError, OSError, asyncio.TimeoutError) as error:
        set_state(model.FAILED, str(error) or type(error).__name__)
        return transfer
    finally:
        if chan:
            await chan.close()


# ── mDNS ───────────────────────────────────────────────────────────────────

def local_ipv4_addresses(exclude_prefixes: tuple[str, ...] = ("docker", "veth", "br-", "virbr",
                                                               "lo", "podman", "cni", "flannel")
                         ) -> list[str]:
    """Addresses worth advertising: real interfaces, not container bridges."""
    import json
    import subprocess
    try:
        out = subprocess.run(["ip", "-j", "-4", "addr"], capture_output=True, text=True,
                             timeout=5, check=True).stdout
        found = []
        for link in json.loads(out):
            if link["ifname"].startswith(exclude_prefixes) or "UP" not in link.get("flags", []):
                continue
            found += [a["local"] for a in link.get("addr_info", []) if a.get("scope") == "global"]
        return found
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        return []


class Advertiser:
    """Announces this machine as a receiver, and keeps announcing.

    A phone does not keep asking: it looks when its share sheet opens and
    otherwise relies on what it has heard, forgetting a receiver it has not
    heard from within a couple of minutes. So the responder in
    :mod:`archdrop.mdns` announces every few seconds as well as answering.
    """

    def __init__(self, name: str, port: int, endpoint_id: str) -> None:
        self.name = name
        self.port = port
        self.endpoint_id = endpoint_id
        self._responder: mdns.Responder | None = None

    def _service(self) -> mdns.Service:
        blob = base64.urlsafe_b64encode(endpoint_info(self.name[:100])).rstrip(b"=").decode("ascii")
        host = re.sub(r"[^A-Za-z0-9-]", "-", socket.gethostname()) or "archdrop"
        return mdns.Service(SERVICE_TYPE, mdns_instance_name(self.endpoint_id), host,
                            self.port, f"n={blob}", local_ipv4_addresses())

    async def start(self) -> None:
        service = self._service()
        if not service.addresses:
            raise RuntimeError("no network interface with an IPv4 address to announce on")
        self._responder = mdns.Responder(service)
        await self._responder.start()

    async def stop(self) -> None:
        if self._responder is not None:
            await self._responder.stop()
            self._responder = None


class Browser:
    """Watches for devices that are willing to receive."""

    def __init__(self, zeroconf, on_change: Callable[[dict[str, model.Peer]], None],
                 own_endpoint_id: str = "") -> None:
        self.zc = zeroconf
        self.on_change = on_change
        self.own_endpoint_id = own_endpoint_id
        self.peers: dict[str, model.Peer] = {}
        self._browser = None
        self._tasks: set[asyncio.Task] = set()
        self._local = set(local_ipv4_addresses())

    async def start(self) -> None:
        from zeroconf import ServiceStateChange
        from zeroconf.asyncio import AsyncServiceBrowser

        def handler(zeroconf, service_type, name, state_change) -> None:
            if state_change is ServiceStateChange.Removed:
                if self.peers.pop(f"qs:{name}", None):
                    self.on_change(self.peers)
                return
            task = asyncio.ensure_future(self._resolve(service_type, name))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

        self._browser = AsyncServiceBrowser(self.zc.zeroconf, SERVICE_TYPE, handlers=[handler])

    async def _resolve(self, service_type: str, name: str) -> None:
        from zeroconf.asyncio import AsyncServiceInfo
        info = AsyncServiceInfo(service_type, name)
        if not await info.async_request(self.zc.zeroconf, 3000):
            return
        instance = name[: -len(service_type) - 1]
        if endpoint_id_from_instance(instance) == self.own_endpoint_id:
            return
        addresses = info.parsed_addresses(version=__import__("zeroconf").IPVersion.V4Only)
        addresses = [a for a in addresses if a not in self._local]
        blob = (info.properties or {}).get(b"n")
        if not addresses or not blob or not info.port:
            return
        try:
            raw = base64.urlsafe_b64decode(blob + b"=" * (-len(blob) % 4))
        except ValueError:
            return
        parsed = parse_endpoint_info(raw)
        if parsed is not None:
            device_name, kind = parsed
        elif len(raw) >= 17:
            # A hidden device still says what kind it is; list it rather than
            # drop it, named by kind and its endpoint id.
            kind = (raw[0] >> 1) & 7
            label = DEVICE_KINDS.get(kind, "device").replace("unknown", "device")
            device_name = f"Android {label} ({endpoint_id_from_instance(instance) or '?'}, name hidden)"
        else:
            return
        self.peers[f"qs:{name}"] = model.Peer(
            id=f"qs:{name}", backend="quickshare", name=device_name,
            kind=DEVICE_KINDS.get(kind, "unknown"), address=addresses[0], port=info.port)
        self.on_change(self.peers)

    async def stop(self) -> None:
        if self._browser is not None:
            await self._browser.async_cancel()
            self._browser = None
        for task in list(self._tasks):
            task.cancel()
