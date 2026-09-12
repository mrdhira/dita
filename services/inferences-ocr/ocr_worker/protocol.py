"""Wire protocol for the OCR worker socket.

Transport is ``AF_UNIX`` / ``SOCK_SEQPACKET``: the kernel preserves message boundaries and
ordering, so a message needs no length prefix -- only a count of the bytes that follow it.

A message is a JSON control block plus an optional binary payload (the image bytes on
``infer``). **Both are chunked**, because a single AF_UNIX datagram cannot exceed
``SO_SNDBUF`` (212992 bytes on a default Linux kernel) -- ``send`` fails with EMSGSIZE
above that, it does not fragment. A dense page yields thousands of lines, so the control
block hits that ceiling as readily as an image does.

    datagram 0     : the prologue -- a small fixed-shape JSON object, always well under
                     the ceiling: {"protocol", "control_len", "payload_len"}
    next datagrams : the control block, in chunks of at most MAX_CHUNK bytes
    next datagrams : the payload, in chunks of at most MAX_CHUNK bytes

A length of 0 means that section sends no datagrams at all. Since no conforming datagram
is ever empty, an empty read means the peer closed.

MAX_CHUNK is the only size a peer has to agree on, and ``handshake`` advertises it along
with the control and payload ceilings. The receive buffer is exactly MAX_CHUNK; a peer
that sends a larger datagram is caught by MSG_TRUNC rather than silently truncated.
"""

from __future__ import annotations

import json
import socket
from typing import Any, Dict, Optional, Tuple

PROTOCOL_VERSION = 2

# Comfortably under the default SO_SNDBUF so a chunk always fits in one datagram.
MAX_CHUNK = 64 * 1024
RECV_BUFFER = MAX_CHUNK

# Ceilings, mostly to keep a malformed peer from making us allocate forever. 8 MiB of
# control block is roughly 70k OCR lines.
MAX_CONTROL = 8 * 1024 * 1024
MAX_PAYLOAD = 64 * 1024 * 1024
MAX_PROLOGUE = 4096

# Seconds. IDLE applies while waiting for the next message on an open connection;
# MESSAGE applies once a prologue has been read and the rest of the message is owed.
IDLE_TIMEOUT = 300.0
MESSAGE_TIMEOUT = 30.0
SEND_TIMEOUT = 30.0


class ProtocolError(Exception):
    """The peer sent something that is not a valid message."""


class Timeout(Exception):
    """The peer went quiet in the middle of a message, or never sent one."""


class PeerGone(Exception):
    """The peer closed the connection."""


def limits() -> Dict[str, int]:
    """What a peer needs to know to talk to us. Advertised by `handshake`."""
    return {
        "max_chunk": MAX_CHUNK,
        "max_control": MAX_CONTROL,
        "max_payload": MAX_PAYLOAD,
        "idle_timeout_s": int(IDLE_TIMEOUT),
        "message_timeout_s": int(MESSAGE_TIMEOUT),
    }


def send_message(
    sock: socket.socket,
    control: Dict[str, Any],
    payload: bytes = b"",
    timeout: Optional[float] = SEND_TIMEOUT,
) -> None:
    control_blob = json.dumps(control, ensure_ascii=False).encode("utf-8")
    if len(control_blob) > MAX_CONTROL:
        raise ProtocolError(f"control block is {len(control_blob)} bytes, over the {MAX_CONTROL} limit")
    if len(payload) > MAX_PAYLOAD:
        raise ProtocolError(f"payload is {len(payload)} bytes, over the {MAX_PAYLOAD} limit")

    prologue = json.dumps(
        {
            "protocol": PROTOCOL_VERSION,
            "control_len": len(control_blob),
            "payload_len": len(payload),
        }
    ).encode("utf-8")

    previous = sock.gettimeout()
    sock.settimeout(timeout)
    try:
        sock.send(prologue)
        _send_chunked(sock, control_blob)
        _send_chunked(sock, payload)
    except TimeoutError as exc:
        raise Timeout("peer stopped reading") from exc
    finally:
        sock.settimeout(previous)


def recv_message(
    sock: socket.socket,
    idle_timeout: Optional[float] = IDLE_TIMEOUT,
    message_timeout: Optional[float] = MESSAGE_TIMEOUT,
) -> Tuple[Dict[str, Any], bytes]:
    raw = _recv_datagram(sock, idle_timeout)
    if not raw:
        raise PeerGone("peer closed the connection")
    if len(raw) > MAX_PROLOGUE:
        raise ProtocolError(f"prologue is {len(raw)} bytes; expected a small JSON header")

    prologue = _decode_json_object(raw, "prologue")
    control_len = _length(prologue, "control_len", MAX_CONTROL)
    payload_len = _length(prologue, "payload_len", MAX_PAYLOAD)
    if control_len == 0:
        raise ProtocolError("prologue announced an empty control block")

    control_blob = _recv_exact(sock, control_len, message_timeout)
    payload = _recv_exact(sock, payload_len, message_timeout)
    return _decode_json_object(control_blob, "control block"), payload


def ok(**fields: Any) -> Dict[str, Any]:
    return {"ok": True, **fields}


def error(code: str, message: str) -> Dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


def _send_chunked(sock: socket.socket, blob: bytes) -> None:
    for start in range(0, len(blob), MAX_CHUNK):
        sock.send(blob[start : start + MAX_CHUNK])


def _recv_datagram(sock: socket.socket, timeout: Optional[float]) -> bytes:
    previous = sock.gettimeout()
    sock.settimeout(timeout)
    try:
        data, _ancillary, flags, _address = sock.recvmsg(RECV_BUFFER)
    except TimeoutError as exc:
        raise Timeout(f"peer sent nothing for {timeout}s") from exc
    finally:
        sock.settimeout(previous)

    if flags & socket.MSG_TRUNC:
        raise ProtocolError(f"peer sent a datagram larger than the {RECV_BUFFER} byte chunk limit")
    return data


def _recv_exact(sock: socket.socket, total: int, timeout: Optional[float]) -> bytes:
    if total == 0:
        return b""

    chunks = []
    received = 0
    while received < total:
        chunk = _recv_datagram(sock, timeout)
        if not chunk:
            raise PeerGone(f"peer closed after {received} of {total} announced bytes")
        chunks.append(chunk)
        received += len(chunk)
    if received != total:
        raise ProtocolError(f"peer sent {received} bytes, prologue announced {total}")
    return b"".join(chunks)


def _decode_json_object(raw: bytes, what: str) -> Dict[str, Any]:
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"{what} is not valid JSON: {exc}") from exc
    if not isinstance(decoded, dict):
        raise ProtocolError(f"{what} must be a JSON object, got {type(decoded).__name__}")
    return decoded


def _length(prologue: Dict[str, Any], field: str, ceiling: int) -> int:
    value = prologue.get(field, 0)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ProtocolError(f"prologue {field} must be a non-negative integer")
    if value > ceiling:
        raise ProtocolError(f"prologue {field} of {value} exceeds the {ceiling} byte limit")
    return value
