"""The receiving end of DIP: one open connection, read a message, answer it.

Binding, the accept loop and any connection cap stay with the service. Those are policy --
how many peers to hold, what to log, when to stop -- and a receiver owns them. What is
protocol, and therefore here, is the exchange on one connection: which framing failure
gets which error code, and the rule that a response we cannot send is still answered
rather than dropped.
"""

from __future__ import annotations

import logging
import socket
from collections.abc import Callable
from typing import Any

from .errors import ErrorCode, error
from .framing import (
    IDLE_TIMEOUT,
    MESSAGE_TIMEOUT,
    PeerGone,
    ProtocolError,
    Timeout,
    recv_message,
    send_message,
)

LOG = logging.getLogger(__name__)

# How long to let a refused peer read its answer before hanging up.
REFUSAL_DRAIN_SECONDS = 0.5

# One request in, one response out. Whatever the handler raises is the service's problem:
# a receiver answers with a code, it does not let an op kill the connection loop.
Handler = Callable[[dict[str, Any], bytes], dict[str, Any]]


def serve_connection(
    connection: socket.socket,
    handle: Handler,
    idle_timeout: float | None = IDLE_TIMEOUT,
    message_timeout: float | None = MESSAGE_TIMEOUT,
    keep_going: Callable[[], bool] | None = None,
) -> None:
    """Answer messages on one connection until the peer goes away or `keep_going` says stop."""
    while keep_going is None or keep_going():
        try:
            control, payload = recv_message(connection, idle_timeout, message_timeout)
        except PeerGone:
            return
        except Timeout as exc:
            LOG.info("closing an idle or stalled connection: %s", exc)
            send_response(connection, error(ErrorCode.timeout, str(exc)))
            return
        except ProtocolError as exc:
            send_response(connection, error(ErrorCode.bad_request, str(exc)))
            return
        except OSError as exc:
            LOG.debug("connection dropped: %s", exc)
            return

        if not send_response(connection, handle(control, payload)):
            return


def send_response(connection: socket.socket, response: dict[str, Any]) -> bool:
    """Send one response. False means the connection is finished with."""
    try:
        send_message(connection, response)
        return True
    except (ProtocolError, Timeout) as exc:
        # The response itself is unsendable (over a ceiling, or the peer stopped reading).
        # Say so in a message that definitely fits rather than dropping the connection.
        LOG.warning("could not send a response: %s", exc)
        try:
            send_message(connection, error(ErrorCode.response_too_large, str(exc)))
        except OSError:
            pass
        return False
    except OSError as exc:
        LOG.debug("could not answer peer: %s", exc)
        return False


def refuse(
    connection: socket.socket,
    response: dict[str, Any],
    drain_seconds: float = REFUSAL_DRAIN_SECONDS,
) -> None:
    """Answer a peer we are not going to serve, and make sure it can read the answer.

    Closing a SOCK_SEQPACKET socket with data still queued resets it, and the peer would
    see a bare ECONNRESET instead of the error code.
    """
    with connection:
        send_response(connection, response)
        try:
            connection.shutdown(socket.SHUT_WR)
            connection.settimeout(drain_seconds)
            while connection.recv(4096):
                pass
        except OSError:
            pass
