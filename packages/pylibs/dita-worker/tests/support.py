"""Fixtures with no engine behind them.

This package must not know what an engine does, so neither may its tests: everything here
is a models.yaml with three made-up models and an engine that records its own lifecycle.
If anything in this directory ever imports from a service, or needs to know what an engine
does, the seam has leaked.
"""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path
from typing import Any, Dict

from dita_worker import Engine, Line, Result, Worker, load_registry
from dita_worker.registry import Registry

# Three models, three engine names, no files to fetch: `system` sources make `load` a
# construction rather than a download, which is what the manager tests are about.
REGISTRY_YAML = """\
version: 1
default_model: alpha
models:
  - id: alpha
    description: the default, and the one a test loads first
    engine: echo
    langs: [en]
    source: {type: system}
  - id: beta
    description: a second model, so a swap has somewhere to go
    engine: reverse
    langs: [en, ja]
    source: {type: system}
  - id: gamma
    description: a third, so "the other one" is never the only alternative
    engine: upper
    langs: [ja]
    source: {type: system}
"""


def write_registry(directory: Path, body: str = REGISTRY_YAML) -> Path:
    path = directory / "models.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def registry_in(directory: Path) -> Registry:
    return load_registry(write_registry(directory))


class FakeEngine(Engine):
    """Records its own lifecycle so a test can see whether it is still alive.

    Every construction is kept, keyed by instance rather than by engine name: keying by
    name would collapse repeated loads of the same engine and hide how many objects a
    leak actually stranded.
    """

    built: list = []
    _registry_lock = threading.Lock()

    def __init__(self, name: str) -> None:
        super().__init__(Path("."), {})
        self.name = name
        self.closed = False
        with FakeEngine._registry_lock:
            FakeEngine.built.append(self)

    @classmethod
    def alive(cls) -> list:
        with cls._registry_lock:
            return [engine for engine in cls.built if not engine.closed]

    @classmethod
    def last(cls, name: str) -> "FakeEngine":
        with cls._registry_lock:
            return [engine for engine in cls.built if engine.name == name][-1]

    def infer(self, payload: bytes) -> Result:
        return Result(text=self.name, lines=[Line(text=self.name, confidence=1.0, box=None)])

    def close(self) -> None:
        self.closed = True


def build_fake_engine(name: str, model_dir: Path, options: Dict[str, Any]) -> FakeEngine:
    """The seam, stubbed: a factory that answers every name with the same fake."""
    return FakeEngine(name)


def fake_worker(registry_path: Path, build_engine=build_fake_engine) -> Worker:
    return Worker(
        name="inferences-fake",
        version="9.9.9",
        engines=("echo", "reverse", "upper"),
        build_engine=build_engine,
        registry_path=registry_path,
        prog="fake_worker",
    )


def wait_until_listening(path: Path, timeout: float = 5.0) -> None:
    """Block until the server at `path` accepts a connection.

    Waiting for the socket *file* is not enough: it appears at bind(), which is before
    listen(), so a connect in that window fails with ECONNREFUSED. Probing with a real
    connection is the only wait that means what the caller wants.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as probe:
                probe.settimeout(timeout)
                probe.connect(str(path))
                return
        except OSError:
            time.sleep(0.02)
    raise AssertionError(f"no server listening at {path} after {timeout}s")
