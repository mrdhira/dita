"""worker -- everything an inference worker does except the inference.

One resident model, a pinned-by-digest fetcher, a models.yaml reader, the DIP socket server
and the CLI. A service supplies an engine factory and a `Worker` describing itself. The wire
is `dip`; nothing here reimplements framing.
"""

from __future__ import annotations

from . import cli
from .engines import Box, Engine, EngineFactory, Line, Result, UnknownEngine
from .fetcher import ChecksumError, FetchError, ensure_model, model_dir
from .health import Health
from .manager import ModelManager, NoModelLoaded
from .metrics import Metrics
from .registry import ModelFile, ModelSpec, Registry, RegistryError, load_registry
from .server import MAX_CONNECTIONS, SocketDirectoryError, SocketServer, dispatch
from .worker import Worker

__version__ = "0.1.0"

__all__ = [
    "Box",
    "ChecksumError",
    "Engine",
    "EngineFactory",
    "FetchError",
    "Health",
    "Line",
    "MAX_CONNECTIONS",
    "Metrics",
    "ModelFile",
    "ModelManager",
    "ModelSpec",
    "NoModelLoaded",
    "Registry",
    "RegistryError",
    "Result",
    "SocketDirectoryError",
    "SocketServer",
    "UnknownEngine",
    "Worker",
    "__version__",
    "cli",
    "dispatch",
    "ensure_model",
    "load_registry",
    "model_dir",
]
