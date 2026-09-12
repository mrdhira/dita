"""The DIP wire framing: a prologue, a chunked control block, a chunked payload.

Transport is ``AF_UNIX`` / ``SOCK_SEQPACKET``, so message boundaries survive and a message
needs no length prefix. Both sections are chunked because a single datagram cannot exceed
``SO_SNDBUF`` (212992 bytes by default) -- ``send`` fails with EMSGSIZE rather than
fragmenting, and a dense page's control block reaches that ceiling as readily as an image.

Every function takes the ``Limits`` in force: a requester frames with what its peer
advertised, never with these defaults. The spec is docs/protocol/[1]dip-specification.md.
"""

from __future__ import annotations

import json
import socket
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

PROTOCOL_VERSION = 2

# Comfortably under the default SO_SNDBUF so a chunk always fits in one datagram. A
# connection frames with the `Limits` it negotiated, not with this.
MAX_CHUNK = 64 * 1024
RECV_BUFFER = MAX_CHUNK

# Ceilings, so a malformed peer cannot make us allocate forever.
MAX_CONTROL = 8 * 1024 * 1024
MAX_PAYLOAD = 64 * 1024 * 1024
MAX_PROLOGUE = 4096

# Seconds. IDLE waits for the next message; MESSAGE applies once a prologue is read.
IDLE_TIMEOUT = 300.0
MESSAGE_TIMEOUT = 30.0
SEND_TIMEOUT = 30.0

# One datagram as the kernel hands it over: the bytes, and whether more arrived than the
# receive buffer could hold. A reader raises Timeout when the peer goes quiet.
DatagramReader = Callable[[float | None], tuple[bytes, bool]]


class ProtocolError(Exception):
    """The peer sent something that is not a valid message."""


class Timeout(Exception):
    """The peer went quiet in the middle of a message, or never sent one."""


class PeerGone(Exception):
    """The peer closed the connection."""


@dataclass(frozen=True)
class Limits:
    """The sizes a connection frames with. Nothing on the wire may be hard-coded: a
    receiver advertises its own in `handshake`, a requester adopts what it is told."""

    max_chunk: int = MAX_CHUNK
    max_control: int = MAX_CONTROL
    max_payload: int = MAX_PAYLOAD
    idle_timeout_s: int = int(IDLE_TIMEOUT)
    message_timeout_s: int = int(MESSAGE_TIMEOUT)

    def as_dict(self) -> dict[str, int]:
        return {
            "max_chunk": self.max_chunk,
            "max_control": self.max_control,
            "max_payload": self.max_payload,
            "idle_timeout_s": self.idle_timeout_s,
            "message_timeout_s": self.message_timeout_s,
        }

    def adopt(self, advertised: Mapping[str, Any]) -> "Limits":
        """These limits with the peer's laid over them. A field left out, or left at zero,
        keeps the current value: a missing limit is not a limit of nothing."""
        known = self.as_dict()
        taken = {
            field: value
            for field, value in advertised.items()
            if field in known and isinstance(value, int) and not isinstance(value, bool) and value > 0
        }
        return replace(self, **taken)


DEFAULT_LIMITS = Limits()


def limits() -> dict[str, int]:
    """What a peer needs to know to talk to us. Advertised by `handshake`."""
    return DEFAULT_LIMITS.as_dict()


def encode_message(
    control: dict[str, Any], payload: bytes = b"", limits: Limits = DEFAULT_LIMITS
) -> list[bytes]:
    """The datagrams one message becomes, prologue first. Over a ceiling is a ProtocolError."""
    control_blob = json.dumps(control, ensure_ascii=False).encode("utf-8")
    if len(control_blob) > limits.max_control:
        raise ProtocolError(
            f"control block is {len(control_blob)} bytes, over the {limits.max_control} limit"
        )
    if len(payload) > limits.max_payload:
        raise ProtocolError(f"payload is {len(payload)} bytes, over the {limits.max_payload} limit")

    prologue = json.dumps(
        {
            "protocol": PROTOCOL_VERSION,
            "control_len": len(control_blob),
            "payload_len": len(payload),
        }
    ).encode("utf-8")
    chunk = limits.max_chunk
    return [prologue, *_chunks(control_blob, chunk), *_chunks(payload, chunk)]


def send_message(
    sock: socket.socket,
    control: dict[str, Any],
    payload: bytes = b"",
    timeout: float | None = SEND_TIMEOUT,
    limits: Limits = DEFAULT_LIMITS,
) -> None:
    datagrams = encode_message(control, payload, limits)

    previous = sock.gettimeout()
    sock.settimeout(timeout)
    try:
        for datagram in datagrams:
            sock.send(datagram)
    except TimeoutError as exc:
        raise Timeout("peer stopped reading") from exc
    finally:
        sock.settimeout(previous)


def recv_message(
    sock: socket.socket,
    idle_timeout: float | None = IDLE_TIMEOUT,
    message_timeout: float | None = MESSAGE_TIMEOUT,
    limits: Limits = DEFAULT_LIMITS,
) -> tuple[dict[str, Any], bytes]:
    return read_message(socket_reader(sock, limits), idle_timeout, message_timeout, limits)


def socket_reader(sock: socket.socket, limits: Limits = DEFAULT_LIMITS) -> DatagramReader:
    """Bind the framing to a real socket. MSG_TRUNC is the kernel's truncation flag."""

    def read(timeout: float | None) -> tuple[bytes, bool]:
        previous = sock.gettimeout()
        sock.settimeout(timeout)
        try:
            data, _ancillary, flags, _address = sock.recvmsg(limits.max_chunk)
        except TimeoutError as exc:
            raise Timeout(f"peer sent nothing for {timeout}s") from exc
        finally:
            sock.settimeout(previous)
        return data, bool(flags & socket.MSG_TRUNC)

    return read


def read_message(
    read: DatagramReader,
    idle_timeout: float | None = IDLE_TIMEOUT,
    message_timeout: float | None = MESSAGE_TIMEOUT,
    limits: Limits = DEFAULT_LIMITS,
) -> tuple[dict[str, Any], bytes]:
    raw = _read_datagram(read, idle_timeout, limits)
    if not raw:
        raise PeerGone("peer closed the connection")
    if len(raw) > MAX_PROLOGUE:
        raise ProtocolError(f"prologue is {len(raw)} bytes; expected a small JSON header")

    prologue = _decode_json_object(raw, "prologue")
    _check_version(prologue)
    control_len = _length(prologue, "control_len", limits.max_control)
    payload_len = _length(prologue, "payload_len", limits.max_payload)
    if control_len == 0:
        raise ProtocolError("prologue announced an empty control block")

    control_blob = _read_exact(read, control_len, message_timeout, limits)
    payload = _read_exact(read, payload_len, message_timeout, limits)
    return _decode_json_object(control_blob, "control block"), payload


def _chunks(blob: bytes, max_chunk: int) -> list[bytes]:
    return [blob[start : start + max_chunk] for start in range(0, len(blob), max_chunk)]


def _read_datagram(read: DatagramReader, timeout: float | None, limits: Limits) -> bytes:
    data, truncated = read(timeout)
    if truncated:
        raise ProtocolError(
            f"peer sent a datagram larger than the {limits.max_chunk} byte chunk limit"
        )
    return data


def _read_exact(read: DatagramReader, total: int, timeout: float | None, limits: Limits) -> bytes:
    if total == 0:
        return b""

    chunks = []
    received = 0
    while received < total:
        chunk = _read_datagram(read, timeout, limits)
        if not chunk:
            raise PeerGone(f"peer closed after {received} of {total} announced bytes")
        chunks.append(chunk)
        received += len(chunk)
    if received != total:
        raise ProtocolError(f"peer sent {received} bytes, prologue announced {total}")
    return b"".join(chunks)


def _decode_json_object(raw: bytes, what: str) -> dict[str, Any]:
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"{what} is not valid JSON: {exc}") from exc
    if not isinstance(decoded, dict):
        raise ProtocolError(f"{what} must be a JSON object, got {type(decoded).__name__}")
    return decoded


def _check_version(prologue: dict[str, Any]) -> None:
    """A version the peer does not announce is this one -- the field is optional. A version
    it announces and we do not speak is the failure this field exists to catch."""
    version = prologue.get("protocol")
    if version is not None and version != PROTOCOL_VERSION:
        raise ProtocolError(
            f"peer speaks protocol {version!r}, this package speaks {PROTOCOL_VERSION}"
        )


def _length(prologue: dict[str, Any], field: str, ceiling: int) -> int:
    if field not in prologue:
        # Defaulting to zero would turn a peer that forgot the payload into a peer that
        # announced an empty one, and the message after it would be read as this one's.
        raise ProtocolError(f"prologue is missing {field}, which every message must announce")
    value = prologue[field]
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ProtocolError(f"prologue {field} must be a non-negative integer")
    if value > ceiling:
        raise ProtocolError(f"prologue {field} of {value} exceeds the {ceiling} byte limit")
    return value
