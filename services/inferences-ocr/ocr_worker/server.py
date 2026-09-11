"""The unix-socket server: AF_UNIX / SOCK_SEQPACKET at $SOCKET_PATH.

The orchestrator owns the queue and the resource budget, so this server keeps no queue
of its own. It accepts connections, and the manager's exclusive lock serialises the work
behind them.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
from pathlib import Path
from typing import Any, Dict

from . import __version__, protocol
from .engines import ENGINE_NAMES, UnknownEngine
from .fetcher import ChecksumError, FetchError
from .manager import ModelManager, NoModelLoaded
from .protocol import PeerGone, ProtocolError, Timeout, error, ok
from .registry import RegistryError

LOG = logging.getLogger(__name__)


class SocketDirectoryError(Exception):
    """The socket directory cannot be used, with an explanation of what to change."""

SERVICE_NAME = "inferences-ocr"
LISTEN_BACKLOG = 16
# Live connections, not just queued ones. Each holds a thread and, mid-message, its
# buffered chunks; the container has a hard memory limit.
MAX_CONNECTIONS = 16
# How long to let a refused peer read its `busy` answer before hanging up. Closing a
# SOCK_SEQPACKET socket with data still queued resets it, and the peer would see a bare
# ECONNRESET instead of the error code.
REFUSAL_DRAIN_SECONDS = 0.5


def dispatch(manager: ModelManager, control: Dict[str, Any], payload: bytes) -> Dict[str, Any]:
    """Turn one request into one response. Pure enough to be reused by the dev HTTP mode."""
    op = control.get("op")

    try:
        if op in ("handshake", "version"):
            return ok(
                service=SERVICE_NAME,
                version=__version__,
                protocol=protocol.PROTOCOL_VERSION,
                limits=protocol.limits(),
                engines=list(ENGINE_NAMES),
                default_model=manager.registry.default_model,
                resident=manager.resident(),
                loading=manager.loading,
            )
        if op == "list":
            return ok(**manager.list())
        if op == "load":
            model_id = control.get("id") or control.get("model")
            if not model_id:
                return error("bad_request", "load needs an `id`")
            return ok(**manager.load(str(model_id)))
        if op == "unload":
            return ok(**manager.unload())
        if op == "infer":
            return ok(**manager.infer(payload))
        return error("bad_request", f"unknown op {op!r}; expected handshake, version, list, load, unload or infer")

    except RegistryError as exc:
        return error("unknown_model", str(exc))
    except UnknownEngine as exc:
        return error("unsupported_engine", str(exc))
    except ChecksumError as exc:
        return error("checksum_mismatch", str(exc))
    except FetchError as exc:
        return error("fetch_failed", str(exc))
    except NoModelLoaded as exc:
        return error("no_model_loaded", str(exc))
    except ValueError as exc:
        return error("bad_request", str(exc))
    except Exception as exc:  # noqa: BLE001 - one bad request must not kill the worker
        LOG.exception("op %r failed", op)
        return error("internal", f"{type(exc).__name__}: {exc}")


class SocketServer:
    def __init__(
        self,
        manager: ModelManager,
        socket_path: Path,
        idle_timeout: float = protocol.IDLE_TIMEOUT,
        message_timeout: float = protocol.MESSAGE_TIMEOUT,
    ) -> None:
        self._manager = manager
        self._socket_path = socket_path
        self._idle_timeout = idle_timeout
        self._message_timeout = message_timeout
        self._sock: socket.socket | None = None
        self._stopping = threading.Event()
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)

    def serve_forever(self) -> None:
        self._sock = self._bind()
        LOG.info("listening on %s (SOCK_SEQPACKET, protocol %d)", self._socket_path, protocol.PROTOCOL_VERSION)
        try:
            while not self._stopping.is_set():
                try:
                    connection, _ = self._sock.accept()
                except OSError:
                    if self._stopping.is_set():
                        break
                    raise
                if not self._slots.acquire(blocking=False):
                    # Inline, not in a thread: we are already at capacity, so throttling
                    # the accept loop here is the point rather than a cost.
                    _refuse(connection)
                    continue

                thread = threading.Thread(
                    target=self._serve_connection, args=(connection,), daemon=True, name="ocr-conn"
                )
                thread.start()
        finally:
            self.close()

    def stop(self) -> None:
        self._stopping.set()
        if self._sock is not None:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None
        self._socket_path.unlink(missing_ok=True)

    def _bind(self) -> socket.socket:
        directory = self._socket_path.parent
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SocketDirectoryError(_unwritable(directory, exc)) from exc
        if not os.access(directory, os.W_OK | os.X_OK):
            raise SocketDirectoryError(_unwritable(directory, "not writable by this user"))

        # A leftover socket file from an unclean exit would make bind fail with EADDRINUSE.
        self._socket_path.unlink(missing_ok=True)

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        sock.bind(str(self._socket_path))
        # The orchestrator runs as a different user in the same pod/compose project.
        os.chmod(self._socket_path, 0o660)
        sock.listen(LISTEN_BACKLOG)
        return sock

    def _serve_connection(self, connection: socket.socket) -> None:
        try:
            with connection:
                while not self._stopping.is_set():
                    try:
                        control, payload = protocol.recv_message(
                            connection, self._idle_timeout, self._message_timeout
                        )
                    except PeerGone:
                        return
                    except Timeout as exc:
                        LOG.info("closing an idle or stalled connection: %s", exc)
                        _try_send(connection, error("timeout", str(exc)))
                        return
                    except ProtocolError as exc:
                        _try_send(connection, error("bad_request", str(exc)))
                        return
                    except OSError as exc:
                        LOG.debug("connection dropped: %s", exc)
                        return

                    response = dispatch(self._manager, control, payload)
                    if not _try_send(connection, response):
                        return
        finally:
            self._slots.release()


def _refuse(connection: socket.socket) -> None:
    """Answer `busy` and make sure the peer can actually read it before closing."""
    LOG.warning("refusing a connection: %d already open", MAX_CONNECTIONS)
    with connection:
        _try_send(connection, error("busy", f"{MAX_CONNECTIONS} connections are already open"))
        try:
            connection.shutdown(socket.SHUT_WR)
            connection.settimeout(REFUSAL_DRAIN_SECONDS)
            while connection.recv(4096):
                pass
        except OSError:
            pass


def _unwritable(directory: Path, reason: object) -> str:
    try:
        stat = directory.stat()
        owned = f"owned by uid {stat.st_uid}, mode {stat.st_mode & 0o777:o}"
    except OSError:
        owned = "missing"
    return (
        f"cannot use the socket directory {directory} ({owned}): {reason}. "
        f"This worker runs as uid {os.getuid()}. If it is a docker named volume that was "
        "first created by another container, remove the volume and let this service start "
        "first (deployment/docker-compose.yml orders it that way), or chown the volume."
    )


def _try_send(connection: socket.socket, response: Dict[str, Any]) -> bool:
    try:
        protocol.send_message(connection, response)
        return True
    except (ProtocolError, Timeout) as exc:
        # The response itself is unsendable (over a ceiling, or the peer stopped reading).
        # Say so in a message that definitely fits rather than dropping the connection.
        LOG.warning("could not send a response: %s", exc)
        try:
            protocol.send_message(connection, error("response_too_large", str(exc)))
        except OSError:
            pass
        return False
    except OSError as exc:
        LOG.debug("could not answer peer: %s", exc)
        return False
