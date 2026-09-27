from archdrop import messages as m
from archdrop import pb, secure


def test_roundtrip_including_negative_and_nested():
    original = {
        "payload_header": {"id": -123456789012345, "type": m.PAYLOAD_FILE, "total_size": 1 << 40,
                           "file_name": "naïve ☃.txt", "is_sensitive": False},
        "payload_chunk": {"flags": 1, "offset": 0, "body": b"\x00\xff" * 10},
        "packet_type": m.PACKET_DATA,
    }
    assert pb.decode(m.PAYLOAD_TRANSFER_FRAME, pb.encode(m.PAYLOAD_TRANSFER_FRAME, original)) == original


def test_repeated_fields_and_unknown_fields_are_skipped():
    schema = {"tags": (1, ["string"])}
    newer = {"tags": (1, ["string"]), "extra": (9, "bytes"), "n": (10, "int64")}
    wire = pb.encode(newer, {"tags": ["a", "b"], "extra": b"zz", "n": 7})
    assert pb.decode(schema, wire) == {"tags": ["a", "b"]}


def test_malformed_input_raises_instead_of_hanging():
    for junk in (b"\x0a\xff", b"\x80", b"\x0a\x05ab", b"\x0b", b"\x00"):
        try:
            pb.decode(m.OFFLINE_FRAME, junk)
        except pb.DecodeError:
            continue
        raise AssertionError(junk)


def test_depth_is_bounded():
    schema: dict = {}
    nested = {"x": (1, schema)}
    schema.update(nested)
    wire = b"\x0a\x00"
    for _ in range(40):
        wire = b"\x0a" + bytes([len(wire)]) + wire if len(wire) < 128 else wire
    try:
        pb.decode(schema, wire)
    except pb.DecodeError:
        pass


def test_public_key_survives_sign_byte_and_short_encodings():
    for _ in range(50):
        key = secure.generate_keypair().public_key()
        again = secure.decode_public_key(secure.encode_public_key(key))
        assert again.public_numbers() == key.public_numbers()


def test_secure_channel_detects_tampering_and_replay():
    a = secure.generate_keypair()
    b = secure.generate_keypair()
    init, _ = secure.client_hello(a)
    server_init = b"server"
    client = secure.derive_keys(a, b.public_key(), init, server_init, is_client=True)
    server = secure.derive_keys(b, a.public_key(), init, server_init, is_client=False)
    assert client.auth_string == server.auth_string and client.pin == server.pin
    tx, rx = secure.SecureChannel(client), secure.SecureChannel(server)

    first, second = tx.seal(b"one"), tx.seal(b"two")
    assert rx.open(first) == b"one"
    try:
        rx.open(first)          # replay: sequence number is now wrong
        raise AssertionError("replay accepted")
    except secure.SecureError:
        pass
    flipped = bytearray(second)
    flipped[-1] ^= 1
    try:
        secure.SecureChannel(server).open(bytes(flipped))
        raise AssertionError("tampering accepted")
    except secure.SecureError:
        pass


def test_pin_is_four_digits_with_signed_byte_semantics():
    assert secure.pin_from_auth_string(bytes(32)) == "0000"
    assert len(secure.pin_from_auth_string(bytes(range(200, 232)))) == 4


def test_hidden_endpoint_is_recognised_not_misread():
    from archdrop.channel import endpoint_is_hidden, parse_endpoint_info
    hidden = bytes.fromhex("32e38f29776b958e90af42df3dfbf24518")   # captured from a phone
    assert endpoint_is_hidden(hidden)
    assert parse_endpoint_info(hidden) is None
    assert (hidden[0] >> 1) & 7 == 1                                  # still says "phone"
