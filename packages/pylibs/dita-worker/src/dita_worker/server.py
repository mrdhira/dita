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
from typing import Any, Dict, Optional

import dip
from dip import ErrorCode, error, ok

from .engines import UnknownEngine
from .fetcher import ChecksumError, FetchError
from .health import Health
from .metrics import Metrics
from .manager import ModelManager, NoModelLoaded
from .registry import RegistryError
from .worker import Worker

LOG = logging.getLogger(__name__)


class SocketDirectoryError(Exception):
    """The socket directory cannot be used, with an explanation of what to change."""


LISTEN_BACKLOG = 16
# Live connections, not just queued ones. Each holds a thread and, mid-message, its
# buffered chunks; the container has a hard memory limit.
MAX_CONNECTIONS = 16

# Kubernetes-shaped probe names, mapped to the Health method that answers them.
PROBE_OPS = {"livez": "live", "readyz": "ready", "startupz": "startup"}
# What this worker implements, which is what `handshake` advertises. Which fields each op
# declares, and the refusal of anything else, belong to the protocol: `dip.validate`.
KNOWN_OPS = ("handshake", "version", "list", "load", "unload", "infer", *PROBE_OPS)


def dispatch(
    worker: Worker,
    manager: ModelManager,
    control: Dict[str, Any],
    payload: bytes,
    health: Optional[Health] = None,
    metrics: Optional[Metrics] = None,
) -> Dict[str, Any]:
    """Turn one request into one response. The socket server is a thin wrapper over this."""
    op = control.get("op")
    response = _dispatch(worker, manager, control, payload, health)
    if metrics is not None:
        name = op if isinstance(op, str) else "unknown"
        if response.get("ok"):
            metrics.op(name, "ok")
        else:
            code = (response.get("error") or {}).get("code", "internal")
            metrics.op(name, "error")
            metrics.error(code)
    return response


def _dispatch(
    worker: Worker,
    manager: ModelManager,
    control: Dict[str, Any],
    payload: bytes,
    health: Optional[Health] = None,
) -> Dict[str, Any]:
    op = control.get("op")

    try:
        refusal = dip.validate(control)
        if refusal is not None:
            return refusal

        if op in ("handshake", "version"):
            return ok(
                service=worker.name,
                version=worker.version,
                protocol=dip.PROTOCOL_VERSION,
                limits=dip.limits(),
                engines=list(worker.engines),
                ops=list(KNOWN_OPS),
                default_model=manager.registry.default_model,
                resident=manager.resident(),
                loading=manager.loading,
            )
        if op in PROBE_OPS:
            probe = getattr(health or Health(manager), PROBE_OPS[op])()
            return {"ok": probe["status"] == "pass", **probe}
        if op == "list":
            return ok(**manager.list())
        if op == "load":
            return ok(**manager.load(str(control.get("id") or control.get("model"))))
        if op == "unload":
            return ok(**manager.unload())
        if op == "infer":
            return ok(**manager.infer(payload))
        # Unreachable while KNOWN_OPS matches dip.OPS; reached the day the protocol
        # declares an op this worker has not implemented yet.
        return error(ErrorCode.internal, f"op {op!r} is declared but not implemented here")

    except RegistryError as exc:
        return error(ErrorCode.unknown_model, str(exc))
    except UnknownEngine as exc:
        return error(ErrorCode.unsupported_engine, str(exc))
    except ChecksumError as exc:
        return error(ErrorCode.checksum_mismatch, str(exc))
    except FetchError as exc:
        return error(ErrorCode.fetch_failed, str(exc))
    except NoModelLoaded as exc:
        return error(ErrorCode.no_model_loaded, str(exc))
    except ValueError as exc:
        return error(ErrorCode.bad_request, str(exc))
    except Exception as exc:  # noqa: BLE001 - one bad request must not kill the worker
        LOG.exception("op %r failed", op)
        return error(ErrorCode.internal, f"{type(exc).__name__}: {exc}")


class SocketServer:
    def __init__(
        self,
        worker: Worker,
        manager: ModelManager,
        socket_path: Path,
        idle_timeout: float = dip.IDLE_TIMEOUT,
        message_timeout: float = dip.MESSAGE_TIMEOUT,
        metrics: Optional[Metrics] = None,
    ) -> None:
        self._worker = worker
        self._manager = manager
        self._socket_path = socket_path
        self._idle_timeout = idle_timeout
        self._message_timeout = message_timeout
        self._sock: socket.socket | None = None
        self._stopping = threading.Event()
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        self._metrics = metrics
        self.health = Health(manager)

    def serve_forever(self) -> None:
        self._sock = self._bind()
        self.health.mark_bound()
        LOG.info("listening on %s (SOCK_SEQPACKET, protocol %d)", self._socket_path, dip.PROTOCOL_VERSION)
        try:
            while not self._stopping.is_set():
                try:
                    connection, _ = self._sock.accept()
                except OSError:
                    if self._stopping.is_set():
                        break
                    raise
                accepted = self._slots.acquire(blocking=False)
                if self._metrics is not None:
                    self._metrics.connection(accepted=accepted)
                if not accepted:
                    # Inline, not in a thread: we are already at capacity, so throttling
                    # the accept loop here is the point rather than a cost.
                    _refuse(connection)
                    continue

                thread = threading.Thread(
                    target=self._serve_connection,
                    args=(connection,),
                    daemon=True,
                    name=f"{self._worker.name}-conn",
                )
                thread.start()
        finally:
            self.close()

    def stop(self) -> None:
        self._stopping.set()
        self.health.mark_stopped()
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
                dip.serve_connection(
                    connection,
                    lambda control, payload: dispatch(
                        self._worker, self._manager, control, payload, self.health, self._metrics
                    ),
                    self._idle_timeout,
                    self._message_timeout,
                    keep_going=lambda: not self._stopping.is_set(),
                )
        finally:
            self._slots.release()


def _refuse(connection: socket.socket) -> None:
    """Answer `busy`; dip drains the socket so the peer can read it before the close."""
    LOG.warning("refusing a connection: %d already open", MAX_CONNECTIONS)
    dip.refuse(connection, error(ErrorCode.busy, f"{MAX_CONNECTIONS} connections are already open"))


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
