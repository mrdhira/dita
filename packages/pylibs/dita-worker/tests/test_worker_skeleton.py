"""A complete worker built out of this package and nothing else.

This is the acceptance test for the seam, and the template for the next service: an engine,
a manifest, a `Worker`, and the framework does the rest. It imports nothing from any
service -- if `inferences-stt` ever needs more than what is in this file, the split was
wrong and this is where that shows up.

The whole lifecycle runs over a real AF_UNIX socket through the real `dip` client, because
"it works in-process" is not the claim being made.
"""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any, Dict

import dip
from dita_worker import (
    Engine,
    Line,
    ModelManager,
    Result,
    SocketServer,
    UnknownEngine,
    Worker,
    load_registry,
)

from .support import wait_until_listening

# A manifest with nothing to download: `system` sources make `load` a construction, so the
# whole test stays offline.
MANIFEST = """\
version: 1
default_model: shouty
models:
  - id: shouty
    description: an engine that needs no weights, so this test needs no network
    engine: shout
    langs: [en]
    source: {type: system}
  - id: mystery
    description: selectable, but this worker has no adapter for it
    engine: whisper
    langs: [en]
    source: {type: system}
"""


class ShoutEngine(Engine):
    """The entire inference half of a worker: bytes in, a Result out."""

    def infer(self, payload: bytes) -> Result:
        text = payload.decode("utf-8").upper()
        return Result(text=text, lines=[Line(text=text, confidence=1.0, box=None)])


def build_engine(name: str, model_dir: Path, options: Dict[str, Any]) -> Engine:
    if name == "shout":
        return ShoutEngine(model_dir, options)
    raise UnknownEngine(f"engine '{name}' is not implemented")


class WorkerSkeletonTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        registry_path = root / "models.yaml"
        registry_path.write_text(MANIFEST, encoding="utf-8")

        # Everything a new service writes for itself, in four statements.
        worker = Worker(
            name="inferences-skeleton",
            version="0.0.1",
            engines=("shout",),
            build_engine=build_engine,
            registry_path=registry_path,
            prog="skeleton_worker",
        )
        manager = ModelManager(load_registry(registry_path), root, worker.build_engine)
        self.socket_path = root / "skeleton.sock"
        server = SocketServer(worker, manager, self.socket_path)

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        # Cleanups run last-registered-first: stop the accept loop, then join it.
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.stop)
        wait_until_listening(self.socket_path)

    def test_a_worker_made_of_nothing_but_this_package_serves_the_whole_lifecycle(self) -> None:
        with dip.Requester.connect(self.socket_path, 10.0) as client:
            hello = client.handshake()
            self.assertTrue(hello["ok"])
            self.assertEqual(hello["service"], "inferences-skeleton")
            self.assertEqual(hello["version"], "0.0.1")
            self.assertEqual(hello["engines"], ["shout"])
            self.assertEqual(hello["protocol"], dip.PROTOCOL_VERSION)
            self.assertIsNone(hello["resident"])

            listing = client.list_models()
            self.assertEqual([model["id"] for model in listing["models"]], ["shouty", "mystery"])
            self.assertEqual(listing["default_model"], "shouty")

            # No implicit loading: infer before load is a coded refusal, not a guess.
            refused = client.infer(b"before any load")
            self.assertFalse(refused["ok"])
            self.assertEqual(refused["error"]["code"], "no_model_loaded")

            loaded = client.load("shouty")
            self.assertTrue(loaded["ok"])
            self.assertEqual(loaded["id"], "shouty")
            self.assertIsNone(loaded["unloaded"])

            answer = client.infer("a quiet sentence".encode("utf-8"))
            self.assertTrue(answer["ok"])
            self.assertEqual(answer["text"], "A QUIET SENTENCE")
            self.assertEqual(answer["model"], "shouty")
            self.assertEqual(answer["lines"][0]["confidence"], 1.0)

            self.assertEqual(client.unload()["unloaded"], "shouty")
            self.assertIsNone(client.handshake()["resident"])

    def test_a_model_whose_engine_the_factory_refuses_comes_back_coded(self) -> None:
        """The framework never names an engine, so this verdict can only come from the seam:
        the factory raises, and the caller is told `unsupported_engine` rather than 500."""
        with dip.Requester.connect(self.socket_path, 10.0) as client:
            self.assertEqual(client.handshake()["engines"], ["shout"])

            refused = client.load("mystery")
            self.assertFalse(refused["ok"])
            self.assertEqual(refused["error"]["code"], "unsupported_engine")
            self.assertIn("whisper", refused["error"]["message"])

            # Nothing resident, and the worker is still serving.
            self.assertIsNone(client.handshake()["resident"])
