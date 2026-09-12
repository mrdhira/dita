"""The requesting end of DIP: connect to a receiver's socket and perform ops.

A failure is a response, not an exception. The receiver answers `{"ok": false, "error":
{"code", "message"}}` and the caller branches on the code; only a connection that breaks
raises. The handshake is explicit rather than automatic, because a one-shot caller -- a
container health probe, say -- sends exactly one message and a forced round trip would
double its cost.

A requester must ignore response fields it does not recognise, which is why nothing here
validates a response against the generated types: that is the one place in this protocol
where tolerance is correct.
"""

from __future__ import annotations

import socket
from pathlib import Path
from types import TracebackType
from typing import Any

from .framing import (
    IDLE_TIMEOUT,
    MESSAGE_TIMEOUT,
    SEND_TIMEOUT,
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

    @classmethod
    def connect(cls, socket_path: Path | str, timeout: float | None = SEND_TIMEOUT) -> "Requester":
        """Dial a receiver. `timeout` covers the connect and every exchange after it;
        construct a Requester directly for timeouts that differ per phase."""
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
        send_message(self._sock, control, payload, self._send_timeout)
        return recv_message(self._sock, self._idle_timeout, self._message_timeout)

    def call(self, op: str, payload: bytes = b"", **fields: Any) -> dict[str, Any]:
        """One op, answered with its control block. No op answers with a payload today."""
        response, _payload = self.exchange({"op": op, **fields}, payload)
        return response

    def handshake(self) -> dict[str, Any]:
        """Once per connection: read `protocol` and take the limits from `limits`."""
        return self.call("handshake")

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
