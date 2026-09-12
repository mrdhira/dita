"""Compatibility shim: the wire protocol lives in `dip` now.

The framing, the error taxonomy and both roles were extracted to `packages/pylibs/dip` so
that this worker and everything that calls it share one implementation rather than two that
drift. Nothing is defined here; the names below are the ones this service used to export,
kept so an existing import still resolves. New code imports `dip` directly.
"""

from __future__ import annotations

from dip import (
    IDLE_TIMEOUT,
    MAX_CHUNK,
    MAX_CONTROL,
    MAX_PAYLOAD,
    MAX_PROLOGUE,
    MESSAGE_TIMEOUT,
    PROTOCOL_VERSION,
    RECV_BUFFER,
    SEND_TIMEOUT,
    PeerGone,
    ProtocolError,
    Timeout,
    error,
    limits,
    ok,
    recv_message,
    send_message,
)

__all__ = [
    "IDLE_TIMEOUT",
    "MAX_CHUNK",
    "MAX_CONTROL",
    "MAX_PAYLOAD",
    "MAX_PROLOGUE",
    "MESSAGE_TIMEOUT",
    "PROTOCOL_VERSION",
    "PeerGone",
    "ProtocolError",
    "RECV_BUFFER",
    "SEND_TIMEOUT",
    "Timeout",
    "error",
    "limits",
    "ok",
    "recv_message",
    "send_message",
]
