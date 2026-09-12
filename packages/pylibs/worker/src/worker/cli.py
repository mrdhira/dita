"""The command line every worker gets: serve, preload, or answer one health probe.

A service's `__main__` is three lines, so the flags, env vars and exit codes stay identical
across workers and an operator learns them once.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Any, Dict

import dip

from . import metrics as metrics_mod
from .manager import ModelManager
from .registry import RegistryError, load_registry
from .server import SocketDirectoryError, SocketServer
from .worker import Worker

LOG = logging.getLogger(__name__)

DESCRIPTION = "One model resident at a time, answered over a unix socket."
DEFAULT_MODELS_DIR = "/models"

# --probe name -> the wire op that answers it. Kubernetes spellings, because the semantics
# are the ones everybody already knows.
PROBES = {"live": "livez", "ready": "readyz", "startup": "startupz"}
PROBE_TIMEOUT = 5.0


def build_parser(worker: Worker) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=worker.prog, description=DESCRIPTION)
    parser.add_argument(
        "--socket",
        default=os.environ.get("SOCKET_PATH", worker.default_socket_path),
        help="unix socket to listen on (env: SOCKET_PATH)",
    )
    parser.add_argument(
        "--models-dir",
        default=os.environ.get("MODELS_DIR", DEFAULT_MODELS_DIR),
        help="where fetched model files live (env: MODELS_DIR)",
    )
    parser.add_argument(
        "--registry",
        default=os.environ.get("MODELS_REGISTRY", str(worker.registry_path)),
        help="path to models.yaml (env: MODELS_REGISTRY)",
    )
    parser.add_argument(
        "--preload",
        metavar="MODEL_ID",
        default=os.environ.get("PRELOAD_MODEL") or None,
        help="load this model at startup instead of waiting for the orchestrator to say so",
    )
    parser.add_argument(
        "--probe",
        choices=sorted(PROBES),
        default=None,
        help=(
            "run one health probe against the socket and exit 0 (pass) or 1 (fail), "
            "instead of starting a server; this is what the container healthcheck execs"
        ),
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get(worker.log_level_env, "info"),
        choices=["debug", "info", "warning", "error"],
    )
    parser.add_argument(
        "--version", action="version", version=f"{worker.name} {worker.version}"
    )
    return parser


def run_probe(socket_path: Path, probe: str) -> int:
    """Ask a running worker one health question over its own socket. Dependency-free on
    purpose: a container healthcheck is an exec probe, so this must work with the stdlib."""
    op = PROBES[probe]
    try:
        with dip.Requester.connect(socket_path, PROBE_TIMEOUT) as requester:
            response = requester.probe(op)
    except (OSError, dip.ProtocolError, dip.Timeout, dip.PeerGone) as exc:
        print(f"{op}: unreachable at {socket_path}: {exc}", file=sys.stderr)
        return 1

    passed = bool(response.get("ok"))
    print(probe_line(op, response))
    return 0 if passed else 1


def probe_line(op: str, response: Dict[str, Any]) -> str:
    """One line: the verdict, then something worth reading. The verdict appears once, and a
    test asserts this exact string."""
    if not response.get("ok"):
        return f"{op}: fail {'; '.join(response.get('reasons') or []) or 'no reason given'}"

    fields = []
    if "resident" in response:
        resident = response["resident"]
        fields.append(f"resident={resident['id'] if resident else 'none'}")
    if response.get("loading"):
        fields.append(f"loading={response['loading']}")
    uptime = response.get("uptime_s")
    if isinstance(uptime, (int, float)):
        fields.append(f"uptime={uptime:.1f}s")

    return f"{op}: pass {' '.join(fields)}".rstrip()


def main(worker: Worker, argv: list[str] | None = None) -> int:
    args = build_parser(worker).parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    if args.probe:
        return run_probe(Path(args.socket), args.probe)

    try:
        registry = load_registry(Path(args.registry))
    except RegistryError as exc:
        LOG.error("%s", exc)
        return 2

    models_dir = Path(args.models_dir)
    models_dir.mkdir(parents=True, exist_ok=True)
    collector = metrics_mod.Metrics()
    manager = ModelManager(registry, models_dir, worker.build_engine, collector)

    if args.preload:
        try:
            LOG.info("preloading %s", args.preload)
            manager.load(args.preload)
        except Exception as exc:  # noqa: BLE001 - report and keep serving; the orchestrator can retry
            LOG.error("preload of %s failed: %s", args.preload, exc)

    server = SocketServer(worker, manager, Path(args.socket), metrics=collector)

    # Its own port, never the DIP socket: a scrape is not the workload. Loopback by
    # default, so it is only reachable elsewhere when something says so.
    try:
        address = metrics_mod.address_from_env()
    except ValueError as exc:
        LOG.error("%s", exc)
        return 2
    metrics_server = metrics_mod.serve(collector, worker.name, manager, address) if address else None

    def shutdown(signum: int, _frame: object) -> None:
        LOG.info("signal %s, shutting down", signal.Signals(signum).name)
        server.stop()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    try:
        server.serve_forever()
    except SocketDirectoryError as exc:
        LOG.error("%s", exc)
        return 3
    finally:
        manager.unload()
        if metrics_server is not None:
            # Returning with the port still bound would leave the next start of this
            # process -- or the next test -- unable to have it.
            metrics_server.shutdown()
            metrics_server.server_close()
    return 0
