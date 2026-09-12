"""The requesting end of DIP: connect to a receiver's socket and perform ops.

A failure is a response, not an exception: the receiver answers with a code and the caller
branches on it; only a broken connection raises. Nothing here validates a response against
the generated types -- a requester must ignore fields it does not recognise, which is the
one place in this protocol where tolerance is correct.
"""

from __future__ import annotations

import socket
from pathlib import Path
from types import TracebackType
from typing import Any

from .framing import (
    DEFAULT_LIMITS,
    IDLE_TIMEOUT,
    MESSAGE_TIMEOUT,
    PROTOCOL_VERSION,
    SEND_TIMEOUT,
    Limits,
    ProtocolError,
    recv_message,
    send_message,
)


class Requester:
    """One connection, held open across calls. Not thread-safe: one exchange at a time."""

    def __init__(
        self,
        sock: socket.socket,
        idle_timeout: float | None = IDLE_TIMEOUT,
        message_timeout: float | None = MESSAGE_TIMEOUT,
        send_timeout: float | None = SEND_TIMEOUT,
    ) -> None:
        self._sock = sock
        self._idle_timeout = idle_timeout
        self._message_timeout = message_timeout
        self._send_timeout = send_timeout
        # This package's defaults until `handshake` answers, then the peer's own.
        self._limits = DEFAULT_LIMITS

    @property
    def limits(self) -> Limits:
        """The sizes this connection frames with right now."""
        return self._limits

    @classmethod
    def connect(cls, socket_path: Path | str, timeout: float | None = SEND_TIMEOUT) -> "Requester":
        """Dial a receiver. `timeout` covers the connect and every exchange after it."""
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        try:
            sock.settimeout(timeout)
            sock.connect(str(socket_path))
        except OSError:
            sock.close()
            raise
        return cls(sock, timeout, timeout, timeout)

    def exchange(
        self, control: dict[str, Any], payload: bytes = b""
    ) -> tuple[dict[str, Any], bytes]:
        """One message out, one message back. The primitive every op below is made of."""
        send_message(self._sock, control, payload, self._send_timeout, self._limits)
        return recv_message(self._sock, self._idle_timeout, self._message_timeout, self._limits)

    def call(self, op: str, payload: bytes = b"", **fields: Any) -> dict[str, Any]:
        """One op, answered with its control block. No op answers with a payload today."""
        response, _payload = self.exchange({"op": op, **fields}, payload)
        return response

    def handshake(self) -> dict[str, Any]:
        """Once per connection: check `protocol` and adopt the limits the peer advertises.
        A peer speaking another wire version raises rather than answering, because every op
        after this one would be framed against a guess."""
        response = self.call("handshake")
        if not response.get("ok"):
            return response

        version = response.get("protocol")
        if version != PROTOCOL_VERSION:
            raise ProtocolError(
                f"peer speaks protocol {version!r}, this package speaks {PROTOCOL_VERSION}"
            )
        advertised = response.get("limits")
        if isinstance(advertised, dict):
            self._limits = self._limits.adopt(advertised)
        return response

    def list_models(self) -> dict[str, Any]:
        return self.call("list")

    def load(self, model_id: str) -> dict[str, Any]:
        return self.call("load", id=model_id)

    def unload(self) -> dict[str, Any]:
        return self.call("unload")

    def infer(self, image: bytes) -> dict[str, Any]:
        """The image is the payload; `infer` takes no fields, and no model."""
        return self.call("infer", image)

    def probe(self, probe: str) -> dict[str, Any]:
        """`livez`, `readyz` or `startupz`, named as the op is named on the wire."""
        return self.call(probe)

    def close(self) -> None:
        self._sock.close()

    def __enter__(self) -> "Requester":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
