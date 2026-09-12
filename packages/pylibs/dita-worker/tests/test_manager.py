"""The one-model-resident invariant, checked on the objects rather than on process RSS.

Scenarios, deliberately not a table: concurrent loads, a failed fetch and a fetch held open
while another thread infers each need their own threads and teardown, and each fails in its
own way. Folding them into a table would put the setup flags in the fixture and the branches
in the loop, and the loop would become the thing under test.
"""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any, Dict

from dita_worker import FetchError, ModelManager, NoModelLoaded, dispatch, fetcher, load_registry

from .support import FakeEngine, build_fake_engine, fake_worker, write_registry


class OneModelResidentTest(unittest.TestCase):
    def setUp(self) -> None:
        FakeEngine.built.clear()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        registry_path = write_registry(Path(directory.name))
        self.registry = load_registry(registry_path)
        self.worker = fake_worker(registry_path)
        self.manager = ModelManager(self.registry, Path("/nonexistent"), build_fake_engine)

        # Fetching is not what this test is about; the engine factory already is a fake.
        self._real_ensure = fetcher.ensure_model
        self.addCleanup(setattr, fetcher, "ensure_model", self._real_ensure)
        fetcher.ensure_model = lambda models_dir, spec, *_: []

    def test_loading_b_closes_a_and_leaves_exactly_one_alive(self) -> None:
        self.manager.load("alpha")
        self.assertEqual(self.manager.resident()["id"], "alpha")

        result = self.manager.load("beta")
        self.assertEqual(result["unloaded"], "alpha")
        self.assertEqual(self.manager.resident()["id"], "beta")

        self.assertEqual([engine.name for engine in FakeEngine.alive()], ["reverse"])
        self.assertTrue(FakeEngine.last("echo").closed)

    def test_already_resident_reports_the_unloaded_field(self) -> None:
        self.manager.load("gamma")
        again = self.manager.load("gamma")
        self.assertTrue(again["already_resident"])
        self.assertIn("unloaded", again)
        self.assertIsNone(again["unloaded"])
        # No second engine was constructed for the same model.
        self.assertEqual(len(FakeEngine.built), 1)

    def test_infer_without_a_loaded_model_fails_clearly(self) -> None:
        with self.assertRaises(NoModelLoaded):
            self.manager.infer(b"image")

        response = dispatch(self.worker, self.manager, {"op": "infer"}, b"image")
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "no_model_loaded")

    def test_unload_leaves_nothing_resident(self) -> None:
        self.manager.load("gamma")
        self.assertEqual(self.manager.unload(), {"unloaded": "gamma"})
        self.assertIsNone(self.manager.resident())
        self.assertTrue(FakeEngine.last("upper").closed)

    # Not a table row: 24 threads and a probe wedged into the engine factory.
    def test_concurrent_loads_never_leave_two_engines_alive(self) -> None:
        """Sample from inside the critical section, not after the lock is released.

        The factory runs while the manager holds its exclusive lock, so counting live
        engines there observes the swap itself rather than a settled global state.
        """
        observed: list = []

        def counting_build(engine: str, model_dir: Path, options: Dict[str, Any]) -> FakeEngine:
            observed.append(len(FakeEngine.alive()))
            return FakeEngine(engine)

        manager = ModelManager(self.registry, Path("/nonexistent"), counting_build)

        ids = ["alpha", "beta", "gamma"] * 8
        threads = [threading.Thread(target=manager.load, args=(i,)) for i in ids]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertTrue(observed)
        # Zero live engines at every construction: the previous one is always released
        # first, so two models are never in memory at once.
        self.assertEqual(max(observed), 0)
        self.assertEqual(len(FakeEngine.alive()), 1)

    def test_a_failed_fetch_does_not_evict_the_resident_model(self) -> None:
        self.manager.load("alpha")
        loaded = FakeEngine.last("echo")

        def failing_fetch(models_dir: Path, spec: Any, *_: Any) -> list:
            raise FetchError("the network went away")

        fetcher.ensure_model = failing_fetch

        with self.assertRaises(FetchError):
            self.manager.load("beta")

        # Still serving the model it had before the failed attempt.
        self.assertEqual(self.manager.resident()["id"], "alpha")
        self.assertFalse(loaded.closed)
        self.assertEqual(self.manager.infer(b"image")["model"], "alpha")

    # Not a table row: two threads rendezvousing on events.
    def test_inference_is_not_blocked_while_a_model_downloads(self) -> None:
        """The exclusive lock must not be held across the fetch."""
        self.manager.load("alpha")

        fetch_started = threading.Event()
        release_fetch = threading.Event()
        inferred_during_fetch = []

        def slow_fetch(models_dir: Path, spec: Any, *_: Any) -> list:
            fetch_started.set()
            release_fetch.wait(timeout=10)
            return []

        fetcher.ensure_model = slow_fetch

        loader = threading.Thread(target=self.manager.load, args=("beta",))
        loader.start()
        self.assertTrue(fetch_started.wait(timeout=10))

        # The old model answers while the new one is still downloading.
        inferred_during_fetch.append(self.manager.infer(b"image")["model"])
        self.assertEqual(self.manager.list()["loading"], "beta")

        release_fetch.set()
        loader.join(timeout=10)

        self.assertEqual(inferred_during_fetch, ["alpha"])
        self.assertEqual(self.manager.resident()["id"], "beta")
