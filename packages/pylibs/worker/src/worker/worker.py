"""What a service tells the framework about itself: the seam between the shared worker
framework and the one object a service supplies. Nothing here imports an engine."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Tuple

from .engines import EngineFactory


@dataclass(frozen=True)
class Worker:
    """The identity a peer sees in the handshake. `engines` is advertised, not enforced: a
    name in models.yaml that `build_engine` refuses is `unsupported_engine` at load time."""

    name: str
    version: str
    engines: Tuple[str, ...]
    build_engine: EngineFactory
    registry_path: Path
    prog: str
    log_level_env: str = "LOG_LEVEL"

    @property
    def default_socket_path(self) -> str:
        """Where the orchestrator looks for this worker unless told otherwise."""
        return f"/run/dita/{self.name}.sock"
