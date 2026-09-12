"""livez / readyz / startupz, and what each one is allowed to be false for.

The three probes answer different questions, so most of these assert that they do *not*
move together. The cold-load scenario at the end is not a table row: it holds a fetch open
in another thread while the probe runs.
"""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any

from worker import FetchError, Health, ModelManager, dispatch, fetcher, load_registry

from .support import FakeEngine, build_fake_engine, fake_worker, write_registry


class HealthTest(unittest.TestCase):
    def setUp(self) -> None:
        FakeEngine.built.clear()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.registry_path = write_registry(Path(self.directory.name))
        self.worker = fake_worker(self.registry_path)

        self._real_ensure = fetcher.ensure_model
        self.addCleanup(setattr, fetcher, "ensure_model", self._real_ensure)
        fetcher.ensure_model = lambda models_dir, spec, *_: []

        self.manager = ModelManager(
            load_registry(self.registry_path), Path(self.directory.name), build_fake_engine
        )
        self.health = Health(self.manager)

    PROBE_OPS = ("livez", "readyz", "startupz")

    def test_a_freshly_bound_worker_passes_all_three(self) -> None:
        self.health.mark_bound()
        for op in self.PROBE_OPS:
            with self.subTest(op):
                response = dispatch(self.worker, self.manager, {"op": op}, b"", self.health)
                self.assertTrue(response["ok"], response.get("reasons"))
                self.assertEqual(response["probe"], op)
                self.assertEqual(response["status"], "pass")
                self.assertEqual(response["reasons"], [])
                self.assertIsInstance(response["uptime_s"], float)

    def test_shutdown_fails_liveness_but_startup_still_reports_a_finished_boot(self) -> None:
        """The probes answer different questions, so they must not move together."""
        self.health.mark_bound()
        self.health.mark_stopped()

        expected = {"livez": False, "readyz": False, "startupz": True}
        for op in self.PROBE_OPS:
            with self.subTest(op):
                response = dispatch(self.worker, self.manager, {"op": op}, b"", self.health)
                self.assertEqual(response["ok"], expected[op], response.get("reasons"))

    def test_startupz_fails_until_the_socket_is_bound(self) -> None:
        before = dispatch(self.worker, self.manager, {"op": "startupz"}, b"", self.health)
        self.assertFalse(before["ok"])
        self.assertIn("not bound", " ".join(before["reasons"]))

        self.health.mark_bound()
        self.assertTrue(
            dispatch(self.worker, self.manager, {"op": "startupz"}, b"", self.health)["ok"]
        )

    def test_livez_ignores_dependencies_and_only_fails_on_shutdown(self) -> None:
        self.health.mark_bound()
        self.manager._last_error = "load of anything failed: boom"
        self.assertTrue(dispatch(self.worker, self.manager, {"op": "livez"}, b"", self.health)["ok"])

        self.health.mark_stopped()
        self.assertFalse(
            dispatch(self.worker, self.manager, {"op": "livez"}, b"", self.health)["ok"]
        )

    def test_readyz_stays_true_with_nothing_loaded_yet(self) -> None:
        """A worker that has simply never been given a model can still be given one."""
        self.health.mark_bound()
        response = dispatch(self.worker, self.manager, {"op": "readyz"}, b"", self.health)
        self.assertTrue(response["ok"])
        self.assertIsNone(response["resident"])

    def test_readyz_goes_false_after_a_failed_load_with_nothing_resident(self) -> None:
        self.health.mark_bound()

        def failing_fetch(models_dir: Path, spec: Any, *_: Any) -> list:
            raise FetchError("the network went away")

        fetcher.ensure_model = failing_fetch
        with self.assertRaises(FetchError):
            self.manager.load("alpha")

        response = dispatch(self.worker, self.manager, {"op": "readyz"}, b"", self.health)
        self.assertFalse(response["ok"])
        self.assertIn("the network went away", " ".join(response["reasons"]))

        # A later successful load clears it: readiness is a current verdict, not a scar.
        fetcher.ensure_model = lambda models_dir, spec, *_: []
        self.manager.load("alpha")
        self.assertTrue(
            dispatch(self.worker, self.manager, {"op": "readyz"}, b"", self.health)["ok"]
        )

    def test_readyz_reports_an_unwritable_models_directory(self) -> None:
        self.health.mark_bound()
        unwritable = Path(self.directory.name) / "locked"
        unwritable.mkdir()
        unwritable.chmod(0o500)
        self.addCleanup(unwritable.chmod, 0o700)

        manager = ModelManager(load_registry(self.registry_path), unwritable, build_fake_engine)
        health = Health(manager)
        health.mark_bound()
        response = dispatch(self.worker, manager, {"op": "readyz"}, b"", health)
        self.assertFalse(response["ok"])
        self.assertIn("not writable", " ".join(response["reasons"]))

    # Not a table row: a fetch held open while the probe runs.
    def test_a_cold_load_in_flight_does_not_trip_readiness(self) -> None:
        """The compose healthcheck must survive a first load that downloads 460 MB."""
        self.health.mark_bound()
        fetch_started = threading.Event()
        release = threading.Event()

        def slow_fetch(models_dir: Path, spec: Any, *_: Any) -> list:
            fetch_started.set()
            release.wait(timeout=10)
            return []

        fetcher.ensure_model = slow_fetch
        loader = threading.Thread(target=self.manager.load, args=("beta",))
        loader.start()
        self.assertTrue(fetch_started.wait(timeout=10))

        response = dispatch(self.worker, self.manager, {"op": "readyz"}, b"", self.health)
        self.assertTrue(response["ok"])
        self.assertEqual(response["loading"], "beta")

        release.set()
        loader.join(timeout=10)
