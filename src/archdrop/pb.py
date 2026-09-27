"""A small protobuf (proto2) wire codec.

Messages are plain dicts described by a schema, so the daemon needs no protoc
and no protobuf runtime. A schema maps a field name to ``(number, type)``;
``type`` is one of the scalar names below, a nested schema, or a one-item list
wrapping either of those for a repeated field::

    HEADER = {"id": (1, "int64"), "name": (2, "string"), "tags": (3, ["string"])}

Encoding writes fields in number order, as protobuf does. Decoding keeps
unknown fields out of the result and skips what it does not know, which is how
a peer running a newer protocol version stays readable.
"""

from __future__ import annotations

import struct
from typing import Any

VARINT, FIXED64, LEN, SGROUP, EGROUP, FIXED32 = 0, 1, 2, 3, 4, 5

_VARINT_TYPES = {"int32", "int64", "uint32", "uint64", "bool", "enum"}
_LEN_TYPES = {"bytes", "string"}

MAX_DEPTH = 16


class DecodeError(ValueError):
    """The bytes are not a well-formed message."""


def _put_varint(out: bytearray, value: int) -> None:
    if value < 0:
        value += 1 << 64
    while True:
        low = value & 0x7F
        value >>= 7
        if value:
            out.append(low | 0x80)
        else:
            out.append(low)
            return


def _get_varint(data: bytes | memoryview, pos: int) -> tuple[int, int]:
    shift = 0
    result = 0
    while True:
        if pos >= len(data):
            raise DecodeError("truncated varint")
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise DecodeError("varint too long")


def _signed64(value: int) -> int:
    return value - (1 << 64) if value >= 1 << 63 else value


def _key(number: int, wire: int) -> int:
    return (number << 3) | wire


def _encode_value(out: bytearray, number: int, kind: Any, value: Any) -> None:
    if isinstance(kind, dict):
        body = encode(kind, value)
        _put_varint(out, _key(number, LEN))
        _put_varint(out, len(body))
        out += body
    elif kind in _VARINT_TYPES:
        _put_varint(out, _key(number, VARINT))
        _put_varint(out, int(value))
    elif kind == "bytes":
        _put_varint(out, _key(number, LEN))
        _put_varint(out, len(value))
        out += value
    elif kind == "string":
        raw = value.encode("utf-8")
        _put_varint(out, _key(number, LEN))
        _put_varint(out, len(raw))
        out += raw
    else:
        raise TypeError(f"unknown field type {kind!r}")


def encode(schema: dict, message: dict) -> bytes:
    """Serialise ``message``; keys absent from the schema are an error."""
    unknown = set(message) - set(schema)
    if unknown:
        raise KeyError(f"not in schema: {sorted(unknown)}")
    out = bytearray()
    for name, (number, kind) in sorted(schema.items(), key=lambda item: item[1][0]):
        value = message.get(name)
        if value is None:
            continue
        if isinstance(kind, list):
            for item in value:
                _encode_value(out, number, kind[0], item)
        else:
            _encode_value(out, number, kind, value)
    return bytes(out)


def _decode_scalar(kind: Any, wire: int, raw: Any) -> Any:
    if isinstance(kind, dict):
        if wire != LEN:
            raise DecodeError("message field with a scalar wire type")
        return decode(kind, raw)
    if kind in _VARINT_TYPES:
        if wire != VARINT:
            raise DecodeError(f"{kind} field with wire type {wire}")
        if kind == "bool":
            return raw != 0
        if kind in ("uint32", "uint64"):
            return raw
        return _signed64(raw)
    if wire != LEN:
        raise DecodeError(f"{kind} field with wire type {wire}")
    if kind == "bytes":
        return bytes(raw)
    try:
        return bytes(raw).decode("utf-8")
    except UnicodeDecodeError as error:
        raise DecodeError("string field is not UTF-8") from error


def decode(schema: dict, data: bytes | memoryview, _depth: int = 0) -> dict:
    """Parse ``data``. Repeated fields always come back as lists."""
    if _depth > MAX_DEPTH:
        raise DecodeError("nesting too deep")
    by_number = {number: (name, kind) for name, (number, kind) in schema.items()}
    out: dict[str, Any] = {}
    view = memoryview(data)
    pos = 0
    while pos < len(view):
        tag, pos = _get_varint(view, pos)
        number, wire = tag >> 3, tag & 7
        if number == 0:
            raise DecodeError("field number 0")
        if wire == VARINT:
            raw, pos = _get_varint(view, pos)
        elif wire == LEN:
            length, pos = _get_varint(view, pos)
            if pos + length > len(view):
                raise DecodeError("truncated field")
            raw = view[pos:pos + length]
            pos += length
        elif wire == FIXED64:
            if pos + 8 > len(view):
                raise DecodeError("truncated fixed64")
            raw = struct.unpack_from("<Q", view, pos)[0]
            pos += 8
        elif wire == FIXED32:
            if pos + 4 > len(view):
                raise DecodeError("truncated fixed32")
            raw = struct.unpack_from("<I", view, pos)[0]
            pos += 4
        else:
            raise DecodeError(f"unsupported wire type {wire}")
        entry = by_number.get(number)
        if entry is None:
            continue
        name, kind = entry
        if isinstance(kind, list):
            inner = kind[0]
            items = out.setdefault(name, [])
            if wire == LEN and not isinstance(inner, dict) and inner in _VARINT_TYPES:
                # A packed run of varints.
                packed = bytes(raw)
                at = 0
                while at < len(packed):
                    value, at = _get_varint(packed, at)
                    items.append(_decode_scalar(inner, VARINT, value))
            else:
                items.append(_decode_scalar(inner, wire, raw) if not isinstance(inner, dict)
                             else decode(inner, raw, _depth + 1))
        elif isinstance(kind, dict):
            if wire != LEN:
                raise DecodeError("message field with a scalar wire type")
            out[name] = decode(kind, raw, _depth + 1)
        else:
            out[name] = _decode_scalar(kind, wire, raw)
    return out
