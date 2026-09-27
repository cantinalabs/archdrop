"""Listen to what other devices say about Quick Share on the network.

``archdrop trace`` is for when a phone does not show this computer as it should.
Open the share sheet on the phone while it runs: the phone's queries for the
Quick Share service, and any receivers it announces itself, appear with their
timing. It only listens; it sends nothing.
"""

from __future__ import annotations

import socket
import struct
import time

from . import quickshare

MDNS_GROUP = "224.0.0.251"


def run(seconds: float, show_all: bool = False) -> int:
    from zeroconf import DNSIncoming

    local = set(quickshare.local_ipv4_addresses())
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.bind(("", 5353))
    for address in local or {"0.0.0.0"}:
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                            socket.inet_aton(MDNS_GROUP) + socket.inet_aton(address))
        except OSError:
            pass
    sock.settimeout(0.5)

    try:
        from .cli import request
        if not request(cmd="status")["state"]["receiving"]:
            print("Note: archdropd is not receiving, so this computer is not being announced.\n"
                  "Run `archdrop on` first, or the phone has nothing to find.\n")
    except Exception:
        print("Note: archdropd is not running, so this computer is not being announced.\n")
    print(f"Listening for {seconds:.0f}s. Open Quick Share on the phone now.\n")
    start = time.time()
    queries: dict[str, int] = {}
    announced: dict[str, str] = {}
    while time.time() - start < seconds:
        try:
            data, (source, _) = sock.recvfrom(9000)
        except socket.timeout:
            continue
        if source in local:
            continue
        try:
            message = DNSIncoming(data)
        except Exception:
            continue
        stamp = f"[{time.time() - start:5.1f}s] {source:<15}"
        asks = [str(q.name) for q in message.questions]
        answers = [(type(r).__name__, str(r.name), r.ttl) for r in message.answers()]
        about_us = [n for n in asks if "FC9F5ED42C8A" in n.upper()]
        theirs = [a for a in answers if "FC9F5ED42C8A" in a[1].upper()]
        if about_us:
            queries[source] = queries.get(source, 0) + 1
            print(f"{stamp} asks for Quick Share receivers")
        for kind, name, ttl in theirs:
            if kind == "DNSService":
                announced[source] = name
            print(f"{stamp} announces {kind} {name} ttl={ttl}")
        if show_all and not (about_us or theirs):
            print(f"{stamp} {'answers' if message.is_response() else 'asks'} {asks or [a[1] for a in answers]}"[:200])

    print("\nsummary")
    if queries:
        for source, count in queries.items():
            print(f"  {source} asked for Quick Share receivers {count} time(s)")
    else:
        print("  no device asked for Quick Share receivers, so the phone is not browsing this "
              "network for them. Common causes:\n"
              "    - the phone drops Wi-Fi while the share sheet is open (recent Pixels and "
              "Galaxys do this when 'Share with Apple devices' is on: turn that off)\n"
              "    - the phone is on another network, or the router isolates Wi-Fi clients\n"
              "    - the phone is looking over Bluetooth only")
    for source, name in announced.items():
        print(f"  {source} is itself advertising as a receiver ({name})")
    return 0
