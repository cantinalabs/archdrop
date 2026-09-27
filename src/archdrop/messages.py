"""Quick Share wire schemas, for :mod:`archdrop.pb`.

Three protocol layers meet on one TCP stream, and each has its own schema:

* Nearby Connections (``OfflineFrame``): connection setup, payload chunking,
  keep-alives. Its field numbers come from Google's open
  ``offline_wire_formats.proto``.
* UKEY2 and SecureMessage: the key exchange and the encrypted envelope every
  frame travels in once the handshake is done.
* Nearby Share (``Frame``): what the two people are actually doing, that is,
  the introduction listing files, the accept or reject, and the pairing frames.

Only the fields this program reads or writes are declared; the rest are skipped
on decode, which is what keeps it working against newer Android builds.
"""

# ── Nearby Connections ─────────────────────────────────────────────────────

# OfflineFrame.Version
OFFLINE_V1 = 1

# V1Frame.FrameType
CONNECTION_REQUEST = 1
CONNECTION_RESPONSE = 2
PAYLOAD_TRANSFER = 3
BANDWIDTH_UPGRADE = 4
KEEP_ALIVE = 5
DISCONNECTION = 6
PAIRED_KEY_ENCRYPTION = 7

# ConnectionRequestFrame.Medium
MEDIUM_WIFI_LAN = 5

# ConnectionResponseFrame.ResponseStatus
RESPONSE_ACCEPT = 1
RESPONSE_REJECT = 2

# OsInfo.OsType
OS_LINUX = 100

# PayloadTransferFrame
PACKET_DATA = 1
PACKET_CONTROL = 2
PAYLOAD_BYTES = 1
PAYLOAD_FILE = 2
PAYLOAD_STREAM = 3
CHUNK_LAST = 1

CONNECTION_REQUEST_FRAME = {
    "endpoint_id": (1, "string"),
    "endpoint_name": (2, "bytes"),
    "handshake_data": (3, "bytes"),
    "nonce": (4, "int32"),
    "mediums": (5, ["enum"]),
    "endpoint_info": (6, "bytes"),
    "keep_alive_interval_millis": (8, "int32"),
    "keep_alive_timeout_millis": (9, "int32"),
    "device_type": (10, "int32"),
    "device_info": (11, "bytes"),
}

OFFLINE_CONNECTION_RESPONSE_FRAME = {
    "status": (1, "int32"),
    "handshake_data": (2, "bytes"),
    "response": (3, "enum"),
    "os_info": (4, {"type": (1, "enum")}),
    "multiplex_socket_bitmask": (5, "int32"),
    "nearby_connections_version": (6, "int32"),
}

PAYLOAD_HEADER = {
    "id": (1, "int64"),
    "type": (2, "enum"),
    "total_size": (3, "int64"),
    "is_sensitive": (4, "bool"),
    "file_name": (5, "string"),
    "parent_folder": (6, "string"),
}

PAYLOAD_CHUNK = {
    "flags": (1, "int32"),
    "offset": (2, "int64"),
    "body": (3, "bytes"),
}

PAYLOAD_TRANSFER_FRAME = {
    "packet_type": (1, "enum"),
    "payload_header": (2, PAYLOAD_HEADER),
    "payload_chunk": (3, PAYLOAD_CHUNK),
    "control_message": (4, {"event": (1, "enum"), "offset": (2, "int64")}),
}

OFFLINE_V1_FRAME = {
    "type": (1, "enum"),
    "connection_request": (2, CONNECTION_REQUEST_FRAME),
    "connection_response": (3, OFFLINE_CONNECTION_RESPONSE_FRAME),
    "payload_transfer": (4, PAYLOAD_TRANSFER_FRAME),
    "keep_alive": (6, {"ack": (1, "bool")}),
    "disconnection": (7, {"request_safe_to_disconnect": (1, "bool"),
                          "ack_safe_to_disconnect": (2, "bool")}),
}

OFFLINE_FRAME = {
    "version": (1, "enum"),
    "v1": (2, OFFLINE_V1_FRAME),
}

# ── UKEY2 ──────────────────────────────────────────────────────────────────

UKEY_ALERT = 1
UKEY_CLIENT_INIT = 2
UKEY_SERVER_INIT = 3
UKEY_CLIENT_FINISH = 4

ALERT_BAD_MESSAGE = 1
ALERT_BAD_MESSAGE_TYPE = 2
ALERT_INCORRECT_MESSAGE = 3
ALERT_BAD_MESSAGE_DATA = 4
ALERT_BAD_VERSION = 100
ALERT_BAD_RANDOM = 101
ALERT_BAD_HANDSHAKE_CIPHER = 102
ALERT_BAD_NEXT_PROTOCOL = 103
ALERT_BAD_PUBLIC_KEY = 104

CIPHER_P256_SHA512 = 100

UKEY2_MESSAGE = {
    "message_type": (1, "enum"),
    "message_data": (2, "bytes"),
}

UKEY2_ALERT = {
    "type": (1, "enum"),
    "error_message": (2, "string"),
}

UKEY2_CLIENT_INIT = {
    "version": (1, "int32"),
    "random": (2, "bytes"),
    "cipher_commitments": (3, [{"handshake_cipher": (1, "enum"), "commitment": (2, "bytes")}]),
    "next_protocol": (4, "string"),
}

UKEY2_SERVER_INIT = {
    "version": (1, "int32"),
    "random": (2, "bytes"),
    "handshake_cipher": (3, "enum"),
    "public_key": (4, "bytes"),
}

UKEY2_CLIENT_FINISHED = {
    "public_key": (1, "bytes"),
}

# ── SecureMessage and the device-to-device layer ───────────────────────────

SIG_HMAC_SHA256 = 1
ENC_AES_256_CBC = 2
PUBLIC_KEY_EC_P256 = 1
GCM_DEVICE_TO_DEVICE_MESSAGE = 13

GENERIC_PUBLIC_KEY = {
    "type": (1, "enum"),
    "ec_p256_public_key": (2, {"x": (1, "bytes"), "y": (2, "bytes")}),
}

SECURE_HEADER = {
    "signature_scheme": (1, "enum"),
    "encryption_scheme": (2, "enum"),
    "verification_key_id": (3, "bytes"),
    "decryption_key_id": (4, "bytes"),
    "iv": (5, "bytes"),
    "public_metadata": (6, "bytes"),
    "associated_data_length": (7, "uint32"),
}

HEADER_AND_BODY = {
    "header": (1, SECURE_HEADER),
    "body": (2, "bytes"),
}

SECURE_MESSAGE = {
    "header_and_body": (1, "bytes"),
    "signature": (2, "bytes"),
}

GCM_METADATA = {
    "type": (1, "enum"),
    "version": (2, "int32"),
}

DEVICE_TO_DEVICE_MESSAGE = {
    "message": (1, "bytes"),
    "sequence_number": (2, "int32"),
}

# ── Nearby Share ───────────────────────────────────────────────────────────

# V1Frame.FrameType
SHARE_INTRODUCTION = 1
SHARE_RESPONSE = 2
SHARE_PAIRED_KEY_ENCRYPTION = 3
SHARE_PAIRED_KEY_RESULT = 4
SHARE_CERTIFICATE_INFO = 5
SHARE_CANCEL = 6

# ConnectionResponseFrame.Status
STATUS_UNKNOWN = 0
STATUS_ACCEPT = 1
STATUS_REJECT = 2
STATUS_NOT_ENOUGH_SPACE = 3
STATUS_UNSUPPORTED_ATTACHMENT = 4
STATUS_TIMED_OUT = 5

# PairedKeyResultFrame.Status
PAIRED_UNABLE = 3

# FileMetadata.Type
FILE_UNKNOWN = 0
FILE_IMAGE = 1
FILE_VIDEO = 2
FILE_APP = 3
FILE_AUDIO = 4

# TextMetadata.Type
TEXT_UNKNOWN = 0
TEXT_TEXT = 1
TEXT_URL = 2
TEXT_ADDRESS = 3
TEXT_PHONE_NUMBER = 4

FILE_METADATA = {
    "name": (1, "string"),
    "type": (2, "enum"),
    "payload_id": (3, "int64"),
    "size": (4, "int64"),
    "mime_type": (5, "string"),
    "id": (6, "int64"),
}

TEXT_METADATA = {
    "text_title": (2, "string"),
    "type": (3, "enum"),
    "payload_id": (4, "int64"),
    "size": (5, "int64"),
    "id": (6, "int64"),
}

WIFI_CREDENTIALS_METADATA = {
    "ssid": (2, "string"),
    "security_type": (3, "enum"),
    "payload_id": (4, "int64"),
    "id": (5, "int64"),
}

INTRODUCTION_FRAME = {
    "file_metadata": (1, [FILE_METADATA]),
    "text_metadata": (2, [TEXT_METADATA]),
    "required_package": (3, "string"),
    "wifi_credentials_metadata": (4, [WIFI_CREDENTIALS_METADATA]),
}

SHARE_V1_FRAME = {
    "type": (1, "enum"),
    "introduction": (2, INTRODUCTION_FRAME),
    "connection_response": (3, {"status": (1, "enum")}),
    "paired_key_encryption": (4, {"signed_data": (1, "bytes"),
                                  "secret_id_hash": (2, "bytes"),
                                  "optional_signed_data": (3, "bytes")}),
    "paired_key_result": (5, {"status": (1, "enum")}),
    "certificate_info": (6, {"public_certificate": (1, [{"secret_id": (1, "bytes")}])}),
}

SHARE_FRAME = {
    "version": (1, "enum"),
    "v1": (2, SHARE_V1_FRAME),
}
