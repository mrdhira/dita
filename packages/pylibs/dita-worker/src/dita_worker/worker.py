"""What a service tells the framework about itself.

This is the seam. Everything below it -- the registry reader, the fetcher, the manager, the
server, the CLI -- is the same code for every inference worker; everything a worker does not
share is in this one object, constructed once at startup and passed down. Nothing here
imports an engine, and nothing under it may.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Tuple

from .engines import EngineFactory


@dataclass(frozen=True)
class Worker:
    """The identity a peer sees in the handshake, plus the one thing only a service knows.

    `engines` is advertised, not enforced: a name in models.yaml that `build_engine` refuses
    is an `unsupported_engine` error at load time, which is where the caller can act on it.
    """

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
