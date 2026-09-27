import asyncio
import base64
import socket
import struct
import time

import pytest
from zeroconf import DNSIncoming

from archdrop import mdns, quickshare


def service(addresses=("10.0.0.5", "10.0.0.6")):
    blob = base64.urlsafe_b64encode(quickshare.endpoint_info("box")).rstrip(b"=").decode()
    return mdns.Service(quickshare.SERVICE_TYPE, quickshare.mdns_instance_name("AbCd"),
                        "box", 4242, f"n={blob}", list(addresses))


def test_announcement_holds_exactly_the_reference_records():
    parsed = DNSIncoming(service().packet())
    kinds = [type(r).__name__ for r in parsed.answers()]
    assert kinds == ["DNSPointer", "DNSService", "DNSText", "DNSAddress", "DNSAddress"]
    assert parsed.is_response()
    assert not parsed.questions
    srv = next(r for r in parsed.answers() if type(r).__name__ == "DNSService")
    assert srv.port == 4242 and srv.server == "box."
    text = next(r for r in parsed.answers() if type(r).__name__ == "DNSText")
    assert text.text[1:3] == b"n="
    ttls = {type(r).__name__: r.ttl for r in parsed.answers()}
    assert ttls["DNSPointer"] == 4500 and ttls["DNSService"] == 120 and ttls["DNSAddress"] == 120


def test_questions_are_matched_case_insensitively_and_by_type():
    s = service()
    assert s.answers_for("_fc9f5ed42c8a._tcp.local.", mdns.TYPE_PTR)
    assert s.answers_for("_FC9F5ED42C8A._tcp.local", mdns.TYPE_ANY)
    assert s.answers_for(s.instance, mdns.TYPE_SRV) == {mdns.TYPE_SRV, mdns.TYPE_TXT, mdns.TYPE_A}
    assert s.answers_for("box.", mdns.TYPE_A) == {mdns.TYPE_A}
    assert s.answers_for("_http._tcp.local.", mdns.TYPE_PTR) is None
    assert s.answers_for(s.service_type, mdns.TYPE_TXT) is None


def test_goodbye_zeroes_every_ttl():
    parsed = DNSIncoming(mdns._zero_ttls(service().packet()))
    assert [r.ttl for r in parsed.answers()] == [0] * 5


def test_labels_are_validated():
    with pytest.raises(ValueError):
        mdns.encode_name("a" * 64 + ".local.")


def test_the_responder_answers_a_real_query():
    addresses = quickshare.local_ipv4_addresses()
    if not addresses:
        pytest.skip("no network interface")

    async def main():
        responder = mdns.Responder(service(addresses[:1]), interval=60)
        await responder.start()
        await asyncio.sleep(3.6)          # past the start-up announcements (0, 1 and 3 s)
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        probe.bind(("", mdns.PORT))
        probe.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                         socket.inet_aton(mdns.GROUP) + socket.inet_aton(addresses[0]))
        probe.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(addresses[0]))
        probe.setblocking(False)
        question = struct.pack(">HHHHHH", 0, 0, 1, 0, 0, 0) + mdns.encode_name(
            quickshare.SERVICE_TYPE) + struct.pack(">HH", mdns.TYPE_PTR, mdns.IN)
        loop = asyncio.get_running_loop()
        probe.sendto(question, (mdns.GROUP, mdns.PORT))
        answered = False
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not answered:
            try:
                data = probe.recv(9000)
            except BlockingIOError:
                await asyncio.sleep(0.05)
                continue
            message = DNSIncoming(data)
            answered = message.is_response() and any(
                type(r).__name__ == "DNSService" for r in message.answers())
        probe.close()
        await responder.stop()
        return answered

    assert asyncio.run(main())
