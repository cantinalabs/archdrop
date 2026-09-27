"""One Nearby Connections link: framing, handshake, encrypted frames.

Everything on the wire is a 4-byte big-endian length and that many bytes. The
first frames are plain (the connection request, the UKEY2 exchange, the two
connection responses); after that every frame is a SecureMessage.

The channel also owns the parts of the protocol that are the same for a sender
and a receiver: replying to keep-alives, reassembling payloads that arrive in
chunks, and telling the two kinds of payload apart. A payload of bytes carries a
Nearby Share frame (or a text attachment); a file payload carries file data.
"""

from __future__ import annotations

import asyncio
import os
import random
import struct
from dataclasses import dataclass
from typing import Any

from . import messages as m
from . import pb, secure

MAX_FRAME = 16 * 1024 * 1024
MAX_BYTES_PAYLOAD = 4 * 1024 * 1024
MAX_OPEN_PAYLOADS = 64
CHUNK_SIZE = 256 * 1024
KEEP_ALIVE_SECONDS = 10


class ProtocolError(Exception):
    """The peer broke the protocol; the transfer cannot continue."""


class Disconnected(Exception):
    """The peer ended the connection, politely or not."""


@dataclass
class BytesPayload:
    payload_id: int
    data: bytes


@dataclass
class FileChunk:
    payload_id: int
    offset: int
    body: bytes
    last: bool


def offline_frame(frame_type: int, **fields: Any) -> bytes:
    return pb.encode(m.OFFLINE_FRAME, {
        "version": m.OFFLINE_V1,
        "v1": {"type": frame_type, **fields},
    })


def share_frame(frame_type: int, **fields: Any) -> bytes:
    return pb.encode(m.SHARE_FRAME, {
        "version": 1,
        "v1": {"type": frame_type, **fields},
    })


def new_payload_id() -> int:
    return random.SystemRandom().randrange(1, 1 << 62)


class Channel:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self.crypto: secure.SecureChannel | None = None
        self._write_lock = asyncio.Lock()
        self._buffers: dict[int, bytearray] = {}
        self._keepalive: asyncio.Task | None = None

    # ── plain frames ───────────────────────────────────────────────────────

    async def read_frame(self) -> bytes:
        try:
            header = await self.reader.readexactly(4)
            (length,) = struct.unpack(">I", header)
            if length > MAX_FRAME:
                raise ProtocolError(f"frame of {length} bytes")
            return await self.reader.readexactly(length)
        except (asyncio.IncompleteReadError, ConnectionError) as error:
            raise Disconnected("connection closed") from error

    async def write_frame(self, data: bytes) -> None:
        async with self._write_lock:
            await self._write_locked(data)

    async def _write_locked(self, data: bytes) -> None:
        try:
            self.writer.write(struct.pack(">I", len(data)) + data)
            await self.writer.drain()
        except (ConnectionError, OSError) as error:
            raise Disconnected("connection closed") from error

    # ── encrypted frames ───────────────────────────────────────────────────

    def enable_encryption(self, keys: secure.SessionKeys) -> None:
        self.crypto = secure.SecureChannel(keys)

    async def send_offline(self, frame: bytes) -> None:
        """Seal and send an OfflineFrame. Sealing and writing share one lock so
        the sequence numbers reach the wire in the order they were issued."""
        assert self.crypto is not None
        async with self._write_lock:
            await self._write_locked(self.crypto.seal(frame))

    async def send_share(self, frame: bytes) -> None:
        """A Nearby Share frame, as a bytes payload in two chunks."""
        header = {"id": new_payload_id(), "type": m.PAYLOAD_BYTES,
                  "total_size": len(frame), "is_sensitive": False}
        for chunk in ({"flags": 0, "offset": 0, "body": frame},
                      {"flags": m.CHUNK_LAST, "offset": len(frame), "body": b""}):
            await self.send_offline(offline_frame(
                m.PAYLOAD_TRANSFER,
                payload_transfer={"packet_type": m.PACKET_DATA,
                                  "payload_header": header, "payload_chunk": chunk}))

    async def send_bytes_payload(self, payload_id: int, data: bytes) -> None:
        header = {"id": payload_id, "type": m.PAYLOAD_BYTES,
                  "total_size": len(data), "is_sensitive": False}
        for chunk in ({"flags": 0, "offset": 0, "body": data},
                      {"flags": m.CHUNK_LAST, "offset": len(data), "body": b""}):
            await self.send_offline(offline_frame(
                m.PAYLOAD_TRANSFER,
                payload_transfer={"packet_type": m.PACKET_DATA,
                                  "payload_header": header, "payload_chunk": chunk}))

    async def send_file_chunk(self, payload_id: int, total: int, name: str,
                              offset: int, body: bytes, last: bool = False) -> None:
        await self.send_offline(offline_frame(
            m.PAYLOAD_TRANSFER,
            payload_transfer={
                "packet_type": m.PACKET_DATA,
                "payload_header": {"id": payload_id, "type": m.PAYLOAD_FILE,
                                   "total_size": total, "is_sensitive": False,
                                   "file_name": name},
                "payload_chunk": {"flags": m.CHUNK_LAST if last else 0,
                                  "offset": offset, "body": body},
            }))

    async def send_disconnection(self) -> None:
        try:
            await self.send_offline(offline_frame(m.DISCONNECTION, disconnection={}))
        except Disconnected:
            pass

    def start_keepalive(self) -> None:
        async def beat() -> None:
            try:
                while True:
                    await asyncio.sleep(KEEP_ALIVE_SECONDS)
                    await self.send_offline(offline_frame(m.KEEP_ALIVE, keep_alive={"ack": False}))
            except (Disconnected, asyncio.CancelledError):
                return
        self._keepalive = asyncio.create_task(beat())

    async def next_event(self) -> BytesPayload | FileChunk:
        """The next payload event. Keep-alives are answered here and a
        disconnection raises :class:`Disconnected`."""
        assert self.crypto is not None
        while True:
            try:
                wire = await self.read_frame()
                decoded = pb.decode(m.OFFLINE_FRAME, self.crypto.open(wire))
            except (secure.SecureError, pb.DecodeError) as error:
                raise ProtocolError(str(error)) from error
            v1 = decoded.get("v1") or {}
            kind = v1.get("type")
            if kind == m.KEEP_ALIVE:
                if not (v1.get("keep_alive") or {}).get("ack"):
                    await self.send_offline(offline_frame(m.KEEP_ALIVE, keep_alive={"ack": True}))
                continue
            if kind == m.DISCONNECTION:
                raise Disconnected("peer disconnected")
            if kind != m.PAYLOAD_TRANSFER:
                continue
            event = self._payload_event(v1.get("payload_transfer") or {})
            if event is not None:
                return event

    def _payload_event(self, transfer: dict) -> BytesPayload | FileChunk | None:
        header = transfer.get("payload_header") or {}
        chunk = transfer.get("payload_chunk")
        if chunk is None:
            return None  # a control message (cancel, ack); the share layer says the rest
        payload_id = header.get("id", 0)
        body = chunk.get("body", b"")
        last = bool(chunk.get("flags", 0) & m.CHUNK_LAST)
        if header.get("type") == m.PAYLOAD_FILE:
            return FileChunk(payload_id, chunk.get("offset", 0), body, last)
        if header.get("type") != m.PAYLOAD_BYTES:
            return None
        if header.get("total_size", 0) > MAX_BYTES_PAYLOAD:
            raise ProtocolError("bytes payload too large")
        buffer = self._buffers.get(payload_id)
        if buffer is None:
            if len(self._buffers) >= MAX_OPEN_PAYLOADS:
                raise ProtocolError("too many open payloads")
            buffer = self._buffers[payload_id] = bytearray()
        if chunk.get("offset", 0) != len(buffer):
            raise ProtocolError("bytes payload chunk out of order")
        buffer += body
        if len(buffer) > MAX_BYTES_PAYLOAD:
            raise ProtocolError("bytes payload too large")
        if last:
            del self._buffers[payload_id]
            return BytesPayload(payload_id, bytes(buffer))
        return None

    async def close(self) -> None:
        if self._keepalive:
            self._keepalive.cancel()
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except (ConnectionError, OSError):
            pass


# ── handshake ──────────────────────────────────────────────────────────────

def endpoint_info(name: str, device_type: int = 3) -> bytes:
    """The blob a device advertises about itself: a flags byte (device type in
    bits 1-3, bit 4 clear meaning visible), 16 random bytes, then the name."""
    raw = name.encode("utf-8")[:255]
    return bytes([device_type << 1]) + os.urandom(16) + bytes([len(raw)]) + raw


HIDDEN_BIT = 0x10


def endpoint_is_hidden(blob: bytes) -> bool:
    """Bit 4 of the flags byte: the device withholds its name.

    Android advertises this way when its visibility is Contacts or Your devices.
    The 17 bytes carry no plaintext name then, only data that the owner's
    contacts can decrypt with certificates from Google's servers."""
    return bool(blob) and bool(blob[0] & HIDDEN_BIT)


def parse_endpoint_info(blob: bytes) -> tuple[str, int] | None:
    if len(blob) < 18 or endpoint_is_hidden(blob):
        return None
    length = blob[17]
    if len(blob) < 18 + length:
        return None
    try:
        return blob[18:18 + length].decode("utf-8"), (blob[0] >> 1) & 7
    except UnicodeDecodeError:
        return None


async def client_handshake(chan: Channel, endpoint_id: str, name: str) -> secure.SessionKeys:
    """Connection request, UKEY2 as the client, then the plain responses."""
    await chan.write_frame(offline_frame(
        m.CONNECTION_REQUEST,
        connection_request={
            "endpoint_id": endpoint_id,
            "endpoint_name": name.encode("utf-8"),
            "endpoint_info": endpoint_info(name),
            "mediums": [m.MEDIUM_WIFI_LAN],
        }))
    private = secure.generate_keypair()
    init, finish = secure.client_hello(private)
    await chan.write_frame(init)

    server_init_wire = await chan.read_frame()
    try:
        outer = pb.decode(m.UKEY2_MESSAGE, server_init_wire)
        if outer.get("message_type") != m.UKEY_SERVER_INIT:
            raise ProtocolError(f"expected ServerInit, got type {outer.get('message_type')}")
        server_init = pb.decode(m.UKEY2_SERVER_INIT, outer.get("message_data", b""))
        if server_init.get("version") != 1 or len(server_init.get("random", b"")) != 32:
            raise ProtocolError("bad ServerInit")
        if server_init.get("handshake_cipher") != m.CIPHER_P256_SHA512:
            raise ProtocolError("server chose a cipher we did not offer")
        peer = secure.decode_public_key(server_init.get("public_key", b""))
        keys = secure.derive_keys(private, peer, init, server_init_wire, is_client=True)
    except (pb.DecodeError, secure.SecureError) as error:
        raise ProtocolError(str(error)) from error

    await chan.write_frame(finish)
    await chan.write_frame(offline_frame(
        m.CONNECTION_RESPONSE,
        connection_response={"response": m.RESPONSE_ACCEPT, "os_info": {"type": m.OS_LINUX}}))
    chan.enable_encryption(keys)

    reply = pb.decode(m.OFFLINE_FRAME, await chan.read_frame())
    v1 = reply.get("v1") or {}
    if v1.get("type") != m.CONNECTION_RESPONSE:
        raise ProtocolError("expected a connection response")
    if (v1.get("connection_response") or {}).get("response") != m.RESPONSE_ACCEPT:
        raise ProtocolError("the device refused the connection")
    return keys


async def server_handshake(chan: Channel) -> tuple[secure.SessionKeys, tuple[str, int]]:
    """As the receiver: read the request, answer UKEY2, exchange responses.

    Returns the keys and the peer's ``(name, device_type)``.
    """
    try:
        request = pb.decode(m.OFFLINE_FRAME, await chan.read_frame())
    except pb.DecodeError as error:
        raise ProtocolError(str(error)) from error
    v1 = request.get("v1") or {}
    if v1.get("type") != m.CONNECTION_REQUEST:
        raise ProtocolError("expected a connection request")
    fields = v1.get("connection_request") or {}
    parsed = parse_endpoint_info(fields.get("endpoint_info", b""))
    if parsed is None:
        name = (fields.get("endpoint_name") or b"").decode("utf-8", "replace").strip()
        parsed = (name or "Unknown device", 0)

    client_init_wire = await chan.read_frame()
    try:
        outer = pb.decode(m.UKEY2_MESSAGE, client_init_wire)
        if outer.get("message_type") != m.UKEY_CLIENT_INIT:
            await chan.write_frame(secure.alert(m.ALERT_BAD_MESSAGE_TYPE))
            raise ProtocolError("expected ClientInit")
        client_init = pb.decode(m.UKEY2_CLIENT_INIT, outer.get("message_data", b""))
    except pb.DecodeError as error:
        await chan.write_frame(secure.alert(m.ALERT_BAD_MESSAGE_DATA))
        raise ProtocolError(str(error)) from error
    if client_init.get("version") != 1:
        await chan.write_frame(secure.alert(m.ALERT_BAD_VERSION))
        raise ProtocolError("unsupported UKEY2 version")
    if len(client_init.get("random", b"")) != 32:
        await chan.write_frame(secure.alert(m.ALERT_BAD_RANDOM))
        raise ProtocolError("bad ClientInit random")
    commitment = next((c.get("commitment") for c in client_init.get("cipher_commitments", [])
                       if c.get("handshake_cipher") == m.CIPHER_P256_SHA512), None)
    if commitment is None:
        await chan.write_frame(secure.alert(m.ALERT_BAD_HANDSHAKE_CIPHER))
        raise ProtocolError("no P-256 cipher offered")
    if client_init.get("next_protocol") != secure.NEXT_PROTOCOL:
        await chan.write_frame(secure.alert(m.ALERT_BAD_NEXT_PROTOCOL))
        raise ProtocolError("unsupported next protocol")

    private = secure.generate_keypair()
    server_init_wire = secure.ukey_message(m.UKEY_SERVER_INIT, {
        "version": 1,
        "random": os.urandom(32),
        "handshake_cipher": m.CIPHER_P256_SHA512,
        "public_key": secure.encode_public_key(private.public_key()),
    }, m.UKEY2_SERVER_INIT)
    await chan.write_frame(server_init_wire)

    finish_wire = await chan.read_frame()
    import hashlib
    import hmac as _hmac
    if not _hmac.compare_digest(hashlib.sha512(finish_wire).digest(), commitment):
        raise ProtocolError("ClientFinished does not match the commitment")
    try:
        outer = pb.decode(m.UKEY2_MESSAGE, finish_wire)
        if outer.get("message_type") != m.UKEY_CLIENT_FINISH:
            raise ProtocolError("expected ClientFinished")
        finished = pb.decode(m.UKEY2_CLIENT_FINISHED, outer.get("message_data", b""))
        peer = secure.decode_public_key(finished.get("public_key", b""))
        keys = secure.derive_keys(private, peer, client_init_wire, server_init_wire, is_client=False)
    except (pb.DecodeError, secure.SecureError) as error:
        raise ProtocolError(str(error)) from error

    reply = pb.decode(m.OFFLINE_FRAME, await chan.read_frame())
    if (reply.get("v1") or {}).get("type") != m.CONNECTION_RESPONSE:
        raise ProtocolError("expected a connection response")
    await chan.write_frame(offline_frame(
        m.CONNECTION_RESPONSE,
        connection_response={"response": m.RESPONSE_ACCEPT, "os_info": {"type": m.OS_LINUX}}))
    chan.enable_encryption(keys)
    return keys, parsed
