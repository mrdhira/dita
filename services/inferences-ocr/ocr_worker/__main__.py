"""Entry point: python -m ocr_worker"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
from pathlib import Path

from . import __version__
from .http_dev import make_server, parse_address
from .manager import ModelManager
from .registry import DEFAULT_REGISTRY_PATH, RegistryError, load_registry
from .server import SocketDirectoryError, SocketServer

LOG = logging.getLogger("ocr_worker")

DEFAULT_SOCKET_PATH = "/run/dita/inferences-ocr.sock"
DEFAULT_MODELS_DIR = "/models"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ocr_worker", description=__doc__)
    parser.add_argument(
        "--socket",
        default=os.environ.get("SOCKET_PATH", DEFAULT_SOCKET_PATH),
        help="unix socket to listen on (env: SOCKET_PATH)",
    )
    parser.add_argument(
        "--models-dir",
        default=os.environ.get("MODELS_DIR", DEFAULT_MODELS_DIR),
        help="where fetched model files live (env: MODELS_DIR)",
    )
    parser.add_argument(
        "--registry",
        default=os.environ.get("MODELS_REGISTRY", str(DEFAULT_REGISTRY_PATH)),
        help="path to models.yaml (env: MODELS_REGISTRY)",
    )
    parser.add_argument(
        "--http",
        metavar="HOST:PORT",
        default=None,
        help="also serve the dev-only HTTP mirror; off by default, manual testing only",
    )
    parser.add_argument(
        "--preload",
        metavar="MODEL_ID",
        default=os.environ.get("PRELOAD_MODEL") or None,
        help="load this model at startup instead of waiting for the orchestrator to say so",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("OCR_LOG_LEVEL", "info"),
        choices=["debug", "info", "warning", "error"],
    )
    parser.add_argument("--version", action="version", version=f"inferences-ocr {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    try:
        registry = load_registry(Path(args.registry))
    except RegistryError as exc:
        LOG.error("%s", exc)
        return 2

    models_dir = Path(args.models_dir)
    models_dir.mkdir(parents=True, exist_ok=True)
    manager = ModelManager(registry, models_dir)

    if args.preload:
        try:
            LOG.info("preloading %s", args.preload)
            manager.load(args.preload)
        except Exception as exc:  # noqa: BLE001 - report and keep serving; the orchestrator can retry
            LOG.error("preload of %s failed: %s", args.preload, exc)

    server = SocketServer(manager, Path(args.socket))
    http_server = None
    if args.http:
        http_server = make_server(manager, parse_address(args.http))
        threading.Thread(target=http_server.serve_forever, daemon=True, name="ocr-http").start()
        LOG.warning("dev HTTP mode is on at %s -- do not enable this in production", args.http)

    def shutdown(signum: int, _frame: object) -> None:
        LOG.info("signal %s, shutting down", signal.Signals(signum).name)
        server.stop()
        if http_server is not None:
            http_server.shutdown()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    try:
        server.serve_forever()
    except SocketDirectoryError as exc:
        LOG.error("%s", exc)
        return 3
    finally:
        manager.unload()
    return 0


if __name__ == "__main__":
    sys.exit(main())
