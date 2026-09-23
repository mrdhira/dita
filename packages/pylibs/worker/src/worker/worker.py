"""What a service tells the framework about itself: the seam between the shared worker
framework and the one object a service supplies. Nothing here imports an engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Tuple

from .engines import EngineFactory
from .routes import Route


@dataclass(frozen=True)
class Worker:
    """The identity a peer sees in the handshake. `engines` is advertised, not enforced: a
    name in models.yaml that `build_engine` refuses is `unsupported_engine` at load time.

    `routes` are served on the metrics port, path to handler, and exist only while that
    port does: `METRICS_ADDR=off` turns them off with it."""

    name: str
    version: str
    engines: Tuple[str, ...]
    build_engine: EngineFactory
    registry_path: Path
    prog: str
    log_level_env: str = "LOG_LEVEL"
    routes: Mapping[str, Route] = field(default_factory=lambda: MappingProxyType({}))

    @property
    def default_socket_path(self) -> str:
        """Where the orchestrator looks for this worker unless told otherwise."""
        return f"/run/dita/{self.name}.sock"
