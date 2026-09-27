# How Quick Share is spoken here

Quick Share (formerly Nearby Share) is not documented by Google as a protocol. It
is three layers Google ships in Android, Chrome OS and Windows, whose message
definitions live in Google's open-source Nearby Connections code and have been
mapped by independent projects ([rquickshare], [NearDrop]). This program speaks
the LAN path of that protocol. Nothing was taken from those projects' code; their
`.proto` files were used as a reference for field numbers, and their
implementations as an independent peer to test against (see "Verification").

## 1. Finding each other: mDNS

A device willing to receive advertises `_FC9F5ED42C8A._tcp.local.`

* **Instance name**: base64url (no padding) of 10 bytes: `0x23`, four ASCII
  characters of endpoint id, the service hash `FC 9F 5E`, and `00 00`.
* **SRV**: the host and the TCP port it listens on.
* **TXT `n`**: base64url of the endpoint info: one flags byte (device type in
  bits 1-3: 0 unknown, 1 phone, 2 tablet, 3 laptop; bit 4 set means hidden), 16
  random bytes, a length byte, and the display name in UTF-8.

Android advertises this only while it is receiving and visible to Everyone, so a
sender that browses and finds nothing usually means the phone is not in that mode.

## 2. The connection: framing and handshake

TCP, and every message is a 4-byte big-endian length followed by that many bytes.

1. Sender -> `OfflineFrame{CONNECTION_REQUEST}` carrying its endpoint id, name and
   endpoint info, mediums `[WIFI_LAN]`.
2. UKEY2 (P-256, `AES_256_CBC-HMAC_SHA256`):
   * Sender -> `ClientInit`: version 1, 32 random bytes, and a commitment to
     `SHA-512(ClientFinished)`.
   * Receiver -> `ServerInit`: 32 random bytes and its public key.
   * Sender -> `ClientFinished`: its public key. The receiver checks the
     commitment against the bytes it receives.
3. Both -> plain `CONNECTION_RESPONSE{ACCEPT}`, then encryption is on.

Public keys are `GenericPublicKey{EC_P256, x, y}` with `x` and `y` as minimal
signed big-endian integers (a leading zero byte when the top bit is set).

**Key schedule.** `secret = SHA-256(ECDH)`; `info = ClientInit || ServerInit` as
they crossed the wire. `auth = HKDF(salt="UKEY2 v1 auth", secret, info)`,
`next = HKDF(salt="UKEY2 v1 next", secret, info)`. Then per direction (`client`,
`server`): `d = HKDF(salt=SHA256("D2D"), next, name)`, and
`enc = HKDF(salt=SHA256("SecureMessage"), d, "ENC:2")`, `sig = ... "SIG:1"`. The
receiver decrypts with the client keys and encrypts with the server keys.

**The PIN** shown on both screens is a rolling hash of `auth` (bytes as signed,
base 31, modulus 9973, C-style remainders), four digits. It is how two people
confirm nobody sits between their devices.

## 3. The envelope

Each later frame is a `SecureMessage{header_and_body, signature}`.
`header_and_body` holds a header (`HMAC_SHA256`, `AES_256_CBC`, a random IV,
`GcmMetadata{DEVICE_TO_DEVICE_MESSAGE, 1}`) and the AES-256-CBC (PKCS#7) body,
which is `DeviceToDeviceMessage{message = OfflineFrame, sequence_number}`. The
signature is HMAC-SHA256 over `header_and_body`. Each direction's counter starts at
1 and must rise by exactly one, so replays and reordering end the session.

## 4. Nearby Connections frames inside

`PAYLOAD_TRANSFER` (chunks of a payload with a header giving id, type and size),
`KEEP_ALIVE` (answered with an ack), `DISCONNECTION`. A payload of type BYTES
carries a Nearby Share frame (or a text attachment); type FILE carries file data.
The last chunk has flag 1 and an empty body.

## 5. Nearby Share frames (inside BYTES payloads)

Both sides send `PAIRED_KEY_ENCRYPTION` first. Each answers the other's with
`PAIRED_KEY_RESULT{UNABLE}` (this program holds no contact certificates, so it
takes part as "Everyone"). The sender then sends `INTRODUCTION` (file names, sizes,
mime types, payload ids; or one text attachment). The receiver answers `RESPONSE`
with ACCEPT, REJECT, NOT_ENOUGH_SPACE, UNSUPPORTED_ATTACHMENT or TIMED_OUT. On
ACCEPT the sender streams each file as FILE payload chunks, then disconnects.

## What is not implemented

* Bluetooth LE wake-up and Bluetooth/Wi-Fi Direct/hotspot transports: both devices
  must be on the same network.
* Contacts-only visibility: the certificate exchange behind it is not done, so the
  phone must offer to Everyone (or, receiving, this machine is an "Everyone"
  device).
* Bandwidth-upgrade negotiation and Wi-Fi credential sharing.
* Folders (send an archive).

## Verification

* `tests/test_loopback.py` runs this sender against this receiver over a socket.
* Against an independent implementation (rquickshare's `rqs_lib`, built from
  source, driven by a small harness): this sender -> that receiver, and that
  sender -> this receiver, each with a 3 MB file plus a small one. Files arrived
  byte-identical and both sides computed the same PIN.
* **Not verified against a real Android phone**: none was available.

[rquickshare]: https://github.com/Martichou/rquickshare
[NearDrop]: https://github.com/grishka/NearDrop
