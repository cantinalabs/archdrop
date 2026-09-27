"""UKEY2 key agreement and the SecureMessage envelope Quick Share rides in.

The handshake is UKEY2 with P-256 and the ``AES_256_CBC-HMAC_SHA256`` next
protocol. The client commits to its key first (a SHA-512 of its ClientFinished
message, sent inside ClientInit), the server answers with its own key, then the
client reveals. Both sides hash the shared secret, expand it with HKDF into an
authentication string and a session secret, and split that into one AES key and
one HMAC key per direction.

The authentication string is what the four-digit PIN is made from. Showing it
on both screens is how two people confirm nobody sits between them.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import os
from dataclasses import dataclass

from cryptography.hazmat.primitives import hashes, padding
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from . import messages as m
from . import pb

NEXT_PROTOCOL = "AES_256_CBC-HMAC_SHA256"

# SHA-256("D2D") and SHA-256("SecureMessage"): the salts the device-to-device
# layer feeds HKDF. Fixed by the protocol.
D2D_SALT = bytes.fromhex("82AA55A0D397F88346CA1CEE8D3909B95F13FA7DEB1D4AB38376B8256DA85510")
KEY_SALT = bytes.fromhex("BF9D2A53C63616D75DB0A7165B91C1EF73E537F2427405FA23610A4BE657642E")


class SecureError(Exception):
    """A frame failed authentication, ordering or format checks."""


def hkdf(salt: bytes, secret: bytes, info: bytes, length: int = 32) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(secret)


# ── Public keys ────────────────────────────────────────────────────────────

def _signed_be(value: int) -> bytes:
    """Minimal two's-complement big-endian, as Java's BigInteger.toByteArray."""
    return value.to_bytes(value.bit_length() // 8 + 1, "big", signed=True)


def generate_keypair() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def encode_public_key(key: ec.EllipticCurvePublicKey) -> bytes:
    """A GenericPublicKey holding this P-256 point."""
    numbers = key.public_numbers()
    return pb.encode(m.GENERIC_PUBLIC_KEY, {
        "type": m.PUBLIC_KEY_EC_P256,
        "ec_p256_public_key": {"x": _signed_be(numbers.x), "y": _signed_be(numbers.y)},
    })


def decode_public_key(data: bytes) -> ec.EllipticCurvePublicKey:
    """Parse a GenericPublicKey, tolerating the sign byte and short encodings."""
    try:
        message = pb.decode(m.GENERIC_PUBLIC_KEY, data)
        point = message["ec_p256_public_key"]
        x = int.from_bytes(point["x"], "big", signed=False)
        y = int.from_bytes(point["y"], "big", signed=False)
        return ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key()
    except (pb.DecodeError, KeyError, ValueError) as error:
        raise SecureError(f"bad public key: {error}") from error


# ── Key schedule ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SessionKeys:
    auth_string: bytes
    encrypt_key: bytes
    send_hmac_key: bytes
    decrypt_key: bytes
    recv_hmac_key: bytes

    @property
    def pin(self) -> str:
        return pin_from_auth_string(self.auth_string)


def derive_keys(private: ec.EllipticCurvePrivateKey, peer: ec.EllipticCurvePublicKey,
                client_init: bytes, server_init: bytes, *, is_client: bool) -> SessionKeys:
    """Turn the ECDH result and the two handshake messages into session keys.

    ``client_init`` and ``server_init`` are the frames exactly as they crossed
    the wire; re-encoding them would change the derivation.
    """
    shared = hashlib.sha256(private.exchange(ec.ECDH(), peer)).digest()
    transcript = client_init + server_init
    auth_string = hkdf(b"UKEY2 v1 auth", shared, transcript)
    next_secret = hkdf(b"UKEY2 v1 next", shared, transcript)

    def direction(name: bytes) -> tuple[bytes, bytes]:
        secret = hkdf(D2D_SALT, next_secret, name)
        return hkdf(KEY_SALT, secret, b"ENC:2"), hkdf(KEY_SALT, secret, b"SIG:1")

    client_enc, client_sig = direction(b"client")
    server_enc, server_sig = direction(b"server")
    if is_client:
        return SessionKeys(auth_string, client_enc, client_sig, server_enc, server_sig)
    return SessionKeys(auth_string, server_enc, server_sig, client_enc, client_sig)


def pin_from_auth_string(auth_string: bytes) -> str:
    """The four digits both screens show.

    A rolling hash over the bytes read as signed, with C-style remainders (the
    sign follows the dividend), so it matches what the phones compute.
    """
    modulus, base = 9973, 31
    value, multiplier = 0, 1
    for byte in auth_string:
        signed = byte - 256 if byte > 127 else byte
        total = value + signed * multiplier
        value = int(math.fmod(total, modulus))
        multiplier = (multiplier * base) % modulus
    return f"{abs(value):04d}"


# ── UKEY2 messages ─────────────────────────────────────────────────────────

def ukey_message(kind: int, body: dict, schema: dict) -> bytes:
    return pb.encode(m.UKEY2_MESSAGE, {"message_type": kind,
                                        "message_data": pb.encode(schema, body)})


def client_hello(private: ec.EllipticCurvePrivateKey) -> tuple[bytes, bytes]:
    """The ClientInit and the ClientFinished it commits to, both as wire bytes."""
    finish = ukey_message(m.UKEY_CLIENT_FINISH,
                          {"public_key": encode_public_key(private.public_key())},
                          m.UKEY2_CLIENT_FINISHED)
    init = ukey_message(m.UKEY_CLIENT_INIT, {
        "version": 1,
        "random": os.urandom(32),
        "cipher_commitments": [{"handshake_cipher": m.CIPHER_P256_SHA512,
                                "commitment": hashlib.sha512(finish).digest()}],
        "next_protocol": NEXT_PROTOCOL,
    }, m.UKEY2_CLIENT_INIT)
    return init, finish


def alert(kind: int) -> bytes:
    return ukey_message(m.UKEY_ALERT, {"type": kind}, m.UKEY2_ALERT)


# ── The envelope ───────────────────────────────────────────────────────────

_GCM_METADATA = pb.encode(m.GCM_METADATA, {"type": m.GCM_DEVICE_TO_DEVICE_MESSAGE, "version": 1})


class SecureChannel:
    """Encrypts and authenticates frames, and checks their order.

    Each direction carries a counter that starts at 1 and rises by one per
    message; a replayed, dropped or reordered frame breaks the count and ends
    the session.
    """

    def __init__(self, keys: SessionKeys) -> None:
        self.keys = keys
        self._send_seq = 0
        self._recv_seq = 0

    def seal(self, offline_frame: bytes) -> bytes:
        self._send_seq += 1
        inner = pb.encode(m.DEVICE_TO_DEVICE_MESSAGE,
                          {"message": offline_frame, "sequence_number": self._send_seq})
        iv = os.urandom(16)
        padder = padding.PKCS7(128).padder()
        encryptor = Cipher(algorithms.AES(self.keys.encrypt_key), modes.CBC(iv)).encryptor()
        body = encryptor.update(padder.update(inner) + padder.finalize()) + encryptor.finalize()
        header_and_body = pb.encode(m.HEADER_AND_BODY, {
            "header": {
                "signature_scheme": m.SIG_HMAC_SHA256,
                "encryption_scheme": m.ENC_AES_256_CBC,
                "iv": iv,
                "public_metadata": _GCM_METADATA,
            },
            "body": body,
        })
        signature = hmac.new(self.keys.send_hmac_key, header_and_body, hashlib.sha256).digest()
        return pb.encode(m.SECURE_MESSAGE, {"header_and_body": header_and_body,
                                            "signature": signature})

    def open(self, wire: bytes) -> bytes:
        """The OfflineFrame inside ``wire``, after every check has passed."""
        try:
            envelope = pb.decode(m.SECURE_MESSAGE, wire)
            header_and_body = envelope["header_and_body"]
            signature = envelope["signature"]
        except (pb.DecodeError, KeyError) as error:
            raise SecureError(f"not a secure message: {error}") from error

        expected = hmac.new(self.keys.recv_hmac_key, header_and_body, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, signature):
            raise SecureError("bad signature")

        try:
            parsed = pb.decode(m.HEADER_AND_BODY, header_and_body)
            iv = parsed["header"]["iv"]
            body = parsed["body"]
            if len(iv) != 16 or len(body) % 16 or not body:
                raise SecureError("bad cipher parameters")
            decryptor = Cipher(algorithms.AES(self.keys.decrypt_key), modes.CBC(iv)).decryptor()
            unpadder = padding.PKCS7(128).unpadder()
            inner = unpadder.update(decryptor.update(body) + decryptor.finalize()) + unpadder.finalize()
            message = pb.decode(m.DEVICE_TO_DEVICE_MESSAGE, inner)
        except SecureError:
            raise
        except (pb.DecodeError, KeyError, ValueError) as error:
            raise SecureError(f"bad message: {error}") from error

        self._recv_seq += 1
        if message.get("sequence_number") != self._recv_seq:
            raise SecureError(f"out of order: got {message.get('sequence_number')}, "
                              f"expected {self._recv_seq}")
        return message.get("message", b"")
