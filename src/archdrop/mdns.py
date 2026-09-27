"""A small mDNS responder for one Quick Share service.

Why not a general library: what an Android phone accepts as a Quick Share
receiver is strict in ways nothing documents, so the packets here are exactly
those of a receiver known to appear on phones (rquickshare): PTR, SRV, TXT and
address records in the answer section, and nothing else. General mDNS libraries
add records of their own (negative NSEC records for hosts and instances), and
those were the first thing that differed from the known-good wire format.

It announces at start, again after 1 s and 3 s, then every ``interval`` seconds
(a phone forgets a receiver it has not heard from), and answers queries for the
service, the instance and the host.
"""

from __future__ import annotations

import asyncio
import logging
import random
import socket
import struct

log = logging.getLogger("archdrop.mdns")

GROUP = "224.0.0.251"
PORT = 5353
IN = 1
CACHE_FLUSH = 0x8000
TYPE_A, TYPE_PTR, TYPE_TXT, TYPE_SRV, TYPE_ANY = 1, 12, 16, 33, 255
SERVICE_TTL, HOST_TTL = 4500, 120


def encode_name(name: str) -> bytes:
    out = b""
    for label in name.rstrip(".").split("."):
        raw = label.encode("utf-8")
        if not 0 < len(raw) < 64:
            raise ValueError(f"bad DNS label {label!r}")
        out += bytes([len(raw)]) + raw
    return out + b"\x00"


def record(name: str, rtype: int, ttl: int, rdata: bytes, unique: bool) -> bytes:
    return (encode_name(name) + struct.pack(">HHIH", rtype, IN | (CACHE_FLUSH if unique else 0),
                                             ttl, len(rdata)) + rdata)


class Service:
    """The records for one advertised service."""

    def __init__(self, service_type: str, instance: str, host: str, port: int,
                 txt: str, addresses: list[str]) -> None:
        self.service_type = service_type.rstrip(".") + "."
        self.instance = f"{instance}.{self.service_type}"
        self.host = host.rstrip(".") + "."
        self.port = port
        self.txt = txt.encode("utf-8")
        self.addresses = addresses
        if len(self.txt) > 255:
            raise ValueError("TXT string too long")

    def records(self, want: set[int] | None = None) -> list[bytes]:
        """Answer records; ``want`` limits them to the types asked for."""
        def wanted(t: int) -> bool:
            return want is None or TYPE_ANY in want or t in want

        out = []
        if wanted(TYPE_PTR):
            out.append(record(self.service_type, TYPE_PTR, SERVICE_TTL,
                              encode_name(self.instance), unique=False))
        if wanted(TYPE_SRV):
            out.append(record(self.instance, TYPE_SRV, HOST_TTL,
                              struct.pack(">HHH", 0, 0, self.port) + encode_name(self.host),
                              unique=True))
        if wanted(TYPE_TXT):
            out.append(record(self.instance, TYPE_TXT, SERVICE_TTL,
                              bytes([len(self.txt)]) + self.txt, unique=True))
        if wanted(TYPE_A):
            out += [record(self.host, TYPE_A, HOST_TTL, socket.inet_aton(a), unique=True)
                    for a in self.addresses]
        return out

    def packet(self, want: set[int] | None = None) -> bytes:
        answers = self.records(want)
        return struct.pack(">HHHHHH", 0, 0x8400, 0, len(answers), 0, 0) + b"".join(answers)

    def answers_for(self, name: str, qtype: int) -> set[int] | None:
        """Which of our records a question wants, or None if it is not for us."""
        name = name.lower().rstrip(".") + "."
        if name == self.service_type.lower():
            return {TYPE_PTR, TYPE_SRV, TYPE_TXT, TYPE_A} if qtype in (TYPE_PTR, TYPE_ANY) else None
        if name == self.instance.lower():
            return {TYPE_SRV, TYPE_TXT, TYPE_A} if qtype in (TYPE_SRV, TYPE_TXT, TYPE_ANY) else None
        if name == self.host.lower():
            return {TYPE_A} if qtype in (TYPE_A, TYPE_ANY) else None
        return None


class Responder:
    def __init__(self, service: Service, interval: float = 10) -> None:
        self.service = service
        self.interval = interval
        self._recv: socket.socket | None = None
        self._senders: list[socket.socket] = []
        self._task: asyncio.Task | None = None
        self._last_reply = 0.0

    def _open(self) -> None:
        addresses = self.service.addresses
        recv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        recv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        recv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        recv.bind(("", PORT))
        for address in addresses:
            try:
                recv.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                                socket.inet_aton(GROUP) + socket.inet_aton(address))
            except OSError:
                pass  # the same group on the same interface twice
            send = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            send.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(address))
            send.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
            send.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
            send.setblocking(False)
            self._senders.append(send)
        recv.setblocking(False)
        self._recv = recv

    def _send(self, packet: bytes) -> None:
        for sender in self._senders:
            try:
                sender.sendto(packet, (GROUP, PORT))
            except OSError as error:
                log.debug("mDNS send failed: %s", error)

    async def start(self) -> None:
        self._open()
        asyncio.get_running_loop().add_reader(self._recv.fileno(), self._on_query)
        self._task = asyncio.create_task(self._announce_forever())

    async def _announce_forever(self) -> None:
        for delay in (0, 1, 2):
            await asyncio.sleep(delay)
            self._send(self.service.packet())
        while True:
            await asyncio.sleep(self.interval)
            self._send(self.service.packet())

    def _on_query(self) -> None:
        assert self._recv is not None
        try:
            data, _ = self._recv.recvfrom(9000)
        except OSError:
            return
        if len(data) < 12 or data[2] & 0x80:      # too short, or a response
            return
        now = asyncio.get_running_loop().time()
        if now - self._last_reply < 0.5:
            return
        want: set[int] = set()
        try:
            from zeroconf import DNSIncoming
            for question in DNSIncoming(data).questions:
                wanted = self.service.answers_for(str(question.name), question.type)
                if wanted:
                    want |= wanted
        except Exception:
            return
        if want:
            self._last_reply = now
            packet = self.service.packet(want)
            asyncio.get_running_loop().call_later(random.uniform(0.02, 0.12), self._send, packet)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None
        if self._recv is not None:
            asyncio.get_running_loop().remove_reader(self._recv.fileno())
            # A goodbye: the same records with a lifetime of zero.
            goodbye = bytearray(self.service.packet())
            self._send(_zero_ttls(bytes(goodbye)))
            self._recv.close()
            self._recv = None
        for sender in self._senders:
            sender.close()
        self._senders = []


def _zero_ttls(packet: bytes) -> bytes:
    """The same packet with every record's TTL set to 0 (an mDNS goodbye)."""
    out = bytearray(packet)
    pos = 12
    (_, _, _, count, _, _) = struct.unpack(">HHHHHH", packet[:12])
    for _ in range(count):
        while out[pos] != 0:
            pos += out[pos] + 1
        pos += 1
        rdlength = struct.unpack(">H", out[pos + 8:pos + 10])[0]
        out[pos + 4:pos + 8] = b"\x00\x00\x00\x00"
        pos += 10 + rdlength
    return bytes(out)
