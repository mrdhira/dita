"""The receiving end of DIP: one open connection, read a message, answer it.

Binding and the accept loop are the service's policy. What is protocol is the exchange on
one connection: which framing failure gets which code, and the rule that a response we
cannot send is still answered -- unless the peer stopped reading part-way through it.
"""

from __future__ import annotations

import logging
import socket
from collections.abc import Callable
from typing import Any

from .errors import ErrorCode, error
from .framing import (
    DEFAULT_LIMITS,
    IDLE_TIMEOUT,
    MESSAGE_TIMEOUT,
    SEND_TIMEOUT,
    Limits,
    PeerGone,
    ProtocolError,
    Timeout,
    recv_message,
    send_message,
)

LOG = logging.getLogger(__name__)

# How long to let a refused peer read its answer before hanging up.
REFUSAL_DRAIN_SECONDS = 0.5

# One request in, one response out. Whatever the handler raises is answered with a code.
Handler = Callable[[dict[str, Any], bytes], dict[str, Any]]


def serve_connection(
    connection: socket.socket,
    handle: Handler,
    idle_timeout: float | None = IDLE_TIMEOUT,
    message_timeout: float | None = MESSAGE_TIMEOUT,
    keep_going: Callable[[], bool] | None = None,
    limits: Limits = DEFAULT_LIMITS,
) -> None:
    """Answer messages on one connection until the peer goes away or `keep_going` says stop."""
    while keep_going is None or keep_going():
        try:
            control, payload = recv_message(connection, idle_timeout, message_timeout, limits)
        except PeerGone:
            return
        except Timeout as exc:
            LOG.info("closing an idle or stalled connection: %s", exc)
            send_response(connection, error(ErrorCode.timeout, str(exc)), limits=limits)
            return
        except ProtocolError as exc:
            send_response(connection, error(ErrorCode.bad_request, str(exc)), limits=limits)
            return
        except OSError as exc:
            LOG.debug("connection dropped: %s", exc)
            return

        if not send_response(connection, handle(control, payload), limits=limits):
            return


def send_response(
    connection: socket.socket,
    response: dict[str, Any],
    send_timeout: float | None = SEND_TIMEOUT,
    limits: Limits = DEFAULT_LIMITS,
) -> bool:
    """Send one response. False means the connection is finished with."""
    try:
        send_message(connection, response, b"", send_timeout, limits)
        return True
    except ProtocolError as exc:
        # `encode_message` refuses before the first datagram, so nothing of this response
        # is on the wire and the peer is owed a whole refusal rather than a dropped socket.
        LOG.warning("could not send a response: %s", exc)
        try:
            send_message(
                connection, error(ErrorCode.response_too_large, str(exc)), b"", send_timeout, limits
            )
        except (OSError, ProtocolError, Timeout) as also:
            # Including Timeout, which is not an OSError: letting it out of here would
            # kill the connection thread with a traceback instead of closing a socket.
            LOG.debug("the refusal could not be sent either: %s", also)
        return False
    except Timeout as exc:
        # The peer stopped reading part-way through this response, so anything sent now
        # would be read as the rest of it. Not `response_too_large`: the response was fine.
        LOG.warning("peer stopped reading a response: %s", exc)
        return False
    except OSError as exc:
        LOG.debug("could not answer peer: %s", exc)
        return False


def refuse(
    connection: socket.socket,
    response: dict[str, Any],
    drain_seconds: float = REFUSAL_DRAIN_SECONDS,
) -> None:
    """Answer a peer we are not going to serve. Closing a SOCK_SEQPACKET socket with data
    still queued resets it, and the peer would see ECONNRESET instead of the error code."""
    with connection:
        send_response(connection, response)
        try:
            connection.shutdown(socket.SHUT_WR)
            connection.settimeout(drain_seconds)
            while connection.recv(4096):
                pass
        except OSError:
            pass
