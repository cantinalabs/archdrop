"""Just enough D-Bus to ask the desktop portal for files, with no dependencies.

The session bus is a Unix socket speaking a binary protocol. This implements
authentication, a marshaller and unmarshaller for the types the portal uses,
method calls, and waiting for one signal: which is everything
:func:`pick_files` needs.
"""

from __future__ import annotations

import os
import secrets
import socket
import struct
import urllib.parse
from typing import Any

CALL, RETURN, ERROR, SIGNAL = 1, 2, 3, 4
F_PATH, F_INTERFACE, F_MEMBER, F_ERROR, F_REPLY_SERIAL, F_DEST, F_SENDER, F_SIG = range(1, 9)
MAX_MESSAGE = 128 * 1024 * 1024

_ALIGN = {"y": 1, "b": 4, "n": 2, "q": 2, "i": 4, "u": 4, "x": 8, "t": 8, "d": 8, "h": 4,
          "s": 4, "o": 4, "g": 1, "a": 4, "(": 8, "{": 8, "v": 1}
_FIXED = {"y": "B", "b": "I", "n": "h", "q": "H", "i": "i", "u": "I", "x": "q", "t": "Q",
          "d": "d", "h": "I"}


class DBusError(Exception):
    pass


def _split(sig: str) -> list[str]:
    """``"sa{sv}(ii)"`` -> ``["s", "a{sv}", "(ii)"]``."""
    out, i = [], 0
    while i < len(sig):
        j = _end(sig, i)
        out.append(sig[i:j])
        i = j
    return out


def _end(sig: str, i: int) -> int:
    c = sig[i]
    if c == "a":
        return _end(sig, i + 1)
    if c in "({":
        close, depth, j = {"(": ")", "{": "}"}[c], 0, i
        while True:
            if sig[j] == c:
                depth += 1
            elif sig[j] == close:
                depth -= 1
                if depth == 0:
                    return j + 1
            j += 1
    return i + 1


class Writer:
    def __init__(self) -> None:
        self.buf = bytearray()

    def pad(self, n: int) -> None:
        self.buf += b"\0" * (-len(self.buf) % n)

    def write(self, sig: str, value: Any) -> None:
        c = sig[0]
        self.pad(_ALIGN[c])
        if c in _FIXED:
            self.buf += struct.pack("<" + _FIXED[c], value)
        elif c in "so":
            raw = value.encode()
            self.buf += struct.pack("<I", len(raw)) + raw + b"\0"
        elif c == "g":
            raw = value.encode()
            self.buf += bytes([len(raw)]) + raw + b"\0"
        elif c == "v":
            inner, val = value
            self.write("g", inner)
            self.write(inner, val)
        elif c == "a":
            item = sig[1:]
            at = len(self.buf)
            self.buf += b"\0\0\0\0"
            self.pad(_ALIGN[item[0]])
            start = len(self.buf)
            items = value.items() if item[0] == "{" else value
            for element in items:
                self.write(item, element)
            struct.pack_into("<I", self.buf, at, len(self.buf) - start)
        elif c in "({":
            for part, val in zip(_split(sig[1:-1]), value):
                self.write(part, val)
        else:
            raise DBusError(f"cannot marshal {sig!r}")


class Reader:
    def __init__(self, data: bytes, pos: int = 0) -> None:
        self.data, self.pos = data, pos

    def pad(self, n: int) -> None:
        self.pos += -self.pos % n

    def read(self, sig: str) -> Any:
        c = sig[0]
        self.pad(_ALIGN[c])
        if c in _FIXED:
            fmt = "<" + _FIXED[c]
            (value,) = struct.unpack_from(fmt, self.data, self.pos)
            self.pos += struct.calcsize(fmt)
            return bool(value) if c == "b" else value
        if c in "so":
            (n,) = struct.unpack_from("<I", self.data, self.pos)
            value = self.data[self.pos + 4:self.pos + 4 + n].decode("utf-8", "replace")
            self.pos += 5 + n
            return value
        if c == "g":
            n = self.data[self.pos]
            value = self.data[self.pos + 1:self.pos + 1 + n].decode()
            self.pos += 2 + n
            return value
        if c == "v":
            inner = self.read("g")
            return self.read(inner)
        if c == "a":
            (n,) = struct.unpack_from("<I", self.data, self.pos)
            self.pos += 4
            item = sig[1:]
            self.pad(_ALIGN[item[0]])
            end = self.pos + n
            if end > len(self.data):
                raise DBusError("array runs past the message")
            out = []
            while self.pos < end:
                out.append(self.read(item))
            return dict(out) if item[0] == "{" else out
        if c in "({":
            return tuple(self.read(part) for part in _split(sig[1:-1]))
        raise DBusError(f"cannot unmarshal {sig!r}")

    def read_all(self, sig: str) -> list:
        return [self.read(part) for part in _split(sig)]


def session_bus_path() -> str:
    address = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
    for part in address.split(";"):
        if part.startswith("unix:"):
            for kv in part[5:].split(","):
                key, _, value = kv.partition("=")
                if key == "path":
                    return urllib.parse.unquote(value)
    return f"/run/user/{os.getuid()}/bus"


class Connection:
    def __init__(self, path: str | None = None) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(path or session_bus_path())
        self._serial = 0
        self._inbox = b""
        # Messages that arrived while waiting for a method reply.
        self.pending: list[tuple[int, dict[int, Any], list]] = []
        self.sock.sendall(b"\0AUTH EXTERNAL " + str(os.getuid()).encode().hex().encode() + b"\r\n")
        reply = self._line()
        if not reply.startswith(b"OK"):
            raise DBusError(f"authentication refused: {reply!r}")
        self.sock.sendall(b"BEGIN\r\n")
        self.name = self.call("org.freedesktop.DBus", "/org/freedesktop/DBus",
                              "org.freedesktop.DBus", "Hello")[0]

    def _line(self) -> bytes:
        while b"\r\n" not in self._inbox:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise DBusError("bus closed during authentication")
            self._inbox += chunk
        line, _, self._inbox = self._inbox.partition(b"\r\n")
        return line

    def _recv_exact(self, n: int) -> bytes:
        while len(self._inbox) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise DBusError("bus closed")
            self._inbox += chunk
        data, self._inbox = self._inbox[:n], self._inbox[n:]
        return data

    def send(self, kind: int, fields: dict[int, tuple[str, Any]], sig: str = "",
             args: tuple = ()) -> int:
        self._serial += 1
        body = Writer()
        for part, value in zip(_split(sig), args):
            body.write(part, value)
        if sig:
            fields[F_SIG] = ("g", sig)
        head = Writer()
        head.buf += b"l" + bytes([kind, 0, 1])
        head.buf += struct.pack("<II", len(body.buf), self._serial)
        head.write("a(yv)", [(code, value) for code, value in fields.items()])
        head.pad(8)
        self.sock.sendall(bytes(head.buf) + bytes(body.buf))
        return self._serial

    def receive(self) -> tuple[int, dict[int, Any], list]:
        fixed = self._recv_exact(16)
        if fixed[0:1] != b"l":
            raise DBusError("big-endian messages are not supported")
        body_len, _serial, fields_len = struct.unpack_from("<III", fixed, 4)
        if body_len + fields_len > MAX_MESSAGE:
            raise DBusError("message too large")
        rest = self._recv_exact(fields_len + (-(16 + fields_len) % 8) + body_len)
        data = fixed + rest
        fields = dict(Reader(data, 12).read("a(yv)"))
        body_at = 16 + fields_len + (-(16 + fields_len) % 8)
        sig = fields.get(F_SIG, "")
        body = Reader(data[body_at:body_at + body_len]).read_all(sig) if sig else []
        return fixed[1], fields, body

    def call(self, dest: str, path: str, interface: str, member: str,
             sig: str = "", *args: Any) -> list:
        serial = self.send(CALL, {F_PATH: ("o", path), F_INTERFACE: ("s", interface),
                                  F_MEMBER: ("s", member), F_DEST: ("s", dest)}, sig, args)
        while True:
            kind, fields, body = self.receive()
            if fields.get(F_REPLY_SERIAL) != serial:
                self.pending.append((kind, fields, body))
                continue
            if kind == ERROR:
                raise DBusError(f"{fields.get(F_ERROR)}: {body[0] if body else ''}")
            return body

    def close(self) -> None:
        self.sock.close()


def pick_files(title: str = "Choose files to send", folder: str | None = None) -> list[str] | None:
    """Ask the desktop's file chooser for files. None if the person cancelled."""
    bus = Connection()
    try:
        token = "archdrop" + secrets.token_hex(6)
        sender = bus.name.lstrip(":").replace(".", "_")
        handle = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
        rule = ("type='signal',interface='org.freedesktop.portal.Request',"
                f"member='Response',path='{handle}'")
        bus.call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                 "AddMatch", "s", rule)
        options: dict[str, tuple[str, Any]] = {"handle_token": ("s", token),
                                                "multiple": ("b", True)}
        if folder:
            options["current_folder"] = ("ay", list(folder.encode() + b"\0"))
        reply = bus.call("org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop",
                         "org.freedesktop.portal.FileChooser", "OpenFile",
                         "ssa{sv}", "", title, options)
        handle = reply[0] if reply else handle
        while True:
            kind, fields, body = bus.pending.pop(0) if bus.pending else bus.receive()
            if kind == SIGNAL and fields.get(F_MEMBER) == "Response" and fields.get(F_PATH) == handle:
                break
        code, results = body[0], body[1]
        if code != 0:
            return None
        paths = []
        for uri in results.get("uris", []):
            parsed = urllib.parse.urlparse(uri)
            if parsed.scheme == "file":
                paths.append(urllib.parse.unquote(parsed.path))
        return paths
    finally:
        bus.close()
