"""DIP -- the Dita Inference Protocol, version dip/2.

One implementation of the wire for every Python peer: the framing, the error taxonomy, and
both roles. Types are generated from `specs/dip/dip.schema.json`; everything else here is
behaviour a schema cannot express, kept honest by `specs/dip/conformance/`.

    from dip import Requester, error, ok, recv_message, send_message

Stdlib only, deliberately: a library every worker links must not drag a supply chain
behind it.
"""

from __future__ import annotations

from . import ops, types
from .errors import ErrorCode, error, ok
from .framing import (
    IDLE_TIMEOUT,
    MAX_CHUNK,
    MAX_CONTROL,
    MAX_PAYLOAD,
    MAX_PROLOGUE,
    MESSAGE_TIMEOUT,
    PROTOCOL_VERSION,
    RECV_BUFFER,
    SEND_TIMEOUT,
    DatagramReader,
    PeerGone,
    ProtocolError,
    Timeout,
    encode_message,
    limits,
    read_message,
    recv_message,
    send_message,
    socket_reader,
)
from .ops import INFER_MODEL_HINT, OP_FIELDS, OPS, PROBE_OPS, validate
from .receiver import Handler, refuse, send_response, serve_connection
from .requester import Requester

__version__ = "0.1.0"

__all__ = [
    "DatagramReader",
    "ErrorCode",
    "Handler",
    "IDLE_TIMEOUT",
    "INFER_MODEL_HINT",
    "MAX_CHUNK",
    "MAX_CONTROL",
    "MAX_PAYLOAD",
    "MAX_PROLOGUE",
    "MESSAGE_TIMEOUT",
    "OPS",
    "OP_FIELDS",
    "PROBE_OPS",
    "PROTOCOL_VERSION",
    "PeerGone",
    "ProtocolError",
    "RECV_BUFFER",
    "Requester",
    "SEND_TIMEOUT",
    "Timeout",
    "__version__",
    "encode_message",
    "error",
    "limits",
    "ok",
    "ops",
    "read_message",
    "recv_message",
    "refuse",
    "send_message",
    "send_response",
    "serve_connection",
    "socket_reader",
    "types",
    "validate",
]
