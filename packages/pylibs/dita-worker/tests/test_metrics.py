"""The metrics surface: the numbers must be real and the scrape must not disturb the work.

A counter that never moves is worse than none, so every assertion here is a before/after
around a real call rather than a check that a name appears in the output.
"""

from __future__ import annotations

import http.client
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

import dip

from dita_worker import Metrics, ModelManager, Result, SocketServer, metrics as metrics_mod

from .support import (
    FakeEngine,
    build_fake_engine,
    fake_worker,
    registry_in,
    wait_until_listening,
    write_registry,
)


def parse(text: str) -> dict:
    """A Prometheus text body into {sample line: float}, comments dropped."""
    samples = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        name, _, value = line.rpartition(" ")
        samples[name] = float(value)
    return samples


class MetricsRenderTest(unittest.TestCase):
    """Rendering, in isolation from a socket."""

    def setUp(self) -> None:
        self.metrics = Metrics()

    def render(self) -> dict:
        return parse(self.metrics.render("inferences-test", None))

    def test_an_untouched_collector_still_reports_the_gauges(self) -> None:
        body = self.render()
        self.assertEqual(body['dita_worker_model_resident{worker="inferences-test"}'], 0)
        self.assertEqual(body['dita_worker_model_loading{worker="inferences-test"}'], 0)
        self.assertIn('process_resident_memory_bytes{worker="inferences-test"}', body)
        self.assertGreater(body['process_resident_memory_bytes{worker="inferences-test"}'], 0)

    def test_every_counter_moves_on_the_path_it_names(self) -> None:
        cases = [
            ("ops", lambda: self.metrics.op("infer", "ok"),
             'dita_worker_ops_total{worker="inferences-test",op="infer",outcome="ok"}'),
            ("errors", lambda: self.metrics.error("no_model_loaded"),
             'dita_worker_errors_total{worker="inferences-test",code="no_model_loaded"}'),
            ("loads", lambda: self.metrics.loaded("alpha", 1.0, None),
             'dita_worker_model_loads_total{worker="inferences-test",model="alpha"}'),
            ("evictions", lambda: self.metrics.loaded("beta", 1.0, "alpha"),
             'dita_worker_model_evictions_total{worker="inferences-test"}'),
            ("fetched bytes", lambda: self.metrics.fetched(4096),
             'dita_worker_fetched_bytes_total{worker="inferences-test"}'),
            ("fetched files", lambda: self.metrics.fetched(1),
             'dita_worker_fetched_files_total{worker="inferences-test"}'),
            ("connections accepted", lambda: self.metrics.connection(True),
             'dita_worker_connections_total{worker="inferences-test",outcome="accepted"}'),
            ("connections refused", lambda: self.metrics.connection(False),
             'dita_worker_connections_total{worker="inferences-test",outcome="refused"}'),
        ]
        for name, action, sample in cases:
            with self.subTest(name):
                before = self.render().get(sample, 0)
                action()
                after = self.render()[sample]
                self.assertGreater(after, before, f"{sample} did not move")

    def test_a_histogram_reports_cumulative_buckets_a_sum_and_a_count(self) -> None:
        for seconds in (0.2, 0.8, 4.0):
            self.metrics.inferred("alpha", seconds)
        body = self.render()

        label = 'worker="inferences-test",model="alpha"'
        self.assertEqual(body[f'dita_worker_infer_duration_seconds_count{{{label}}}'], 3)
        self.assertAlmostEqual(body[f'dita_worker_infer_duration_seconds_sum{{{label}}}'], 5.0, 3)
        # Cumulative: everything at or under the edge.
        self.assertEqual(body[f'dita_worker_infer_duration_seconds_bucket{{{label},le="0.25"}}'], 1)
        self.assertEqual(body[f'dita_worker_infer_duration_seconds_bucket{{{label},le="1.0"}}'], 2)
        self.assertEqual(body[f'dita_worker_infer_duration_seconds_bucket{{{label},le="+Inf"}}'], 3)

    def test_the_body_is_valid_prometheus_text(self) -> None:
        self.metrics.op("infer", "ok")
        self.metrics.inferred("alpha", 0.5)
        text = self.metrics.render("inferences-test", None)

        for name in ("dita_worker_ops_total", "dita_worker_infer_duration_seconds"):
            self.assertIn(f"# HELP {name} ", text)
            self.assertIn(f"# TYPE {name} ", text)
        self.assertTrue(text.endswith("\n"))
        for line in text.splitlines():
            if line.startswith("#"):
                continue
            self.assertRegex(line, r"^[a-zA-Z_:][a-zA-Z0-9_:]*(\{.*\})? -?[0-9.eE+]+$", line)

    def test_a_label_value_with_a_quote_cannot_break_the_format(self) -> None:
        self.metrics.error('weird"code')
        text = self.metrics.render("inferences-test", None)
        self.assertIn(r'code="weird\"code"', text)


class MetricsEndpointTest(unittest.TestCase):
    """The HTTP surface, and the rule that a scrape never waits on the worker's lock."""

    def start(self, manager=None):
        self.metrics = Metrics()
        server = metrics_mod.serve(self.metrics, "inferences-test", manager, ("127.0.0.1", 0))
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address

    def scrape(self, address, path: str = "/metrics"):
        conn = http.client.HTTPConnection(*address, timeout=5)
        try:
            conn.request("GET", path)
            response = conn.getresponse()
            return response.status, response.getheader("Content-Type"), response.read().decode()
        finally:
            conn.close()

    def test_it_serves_prometheus_text(self) -> None:
        address = self.start()
        status, content_type, body = self.scrape(address)

        self.assertEqual(status, 200)
        self.assertIn("text/plain", content_type)
        self.assertIn("version=0.0.4", content_type)
        self.assertIn("dita_worker_uptime_seconds", body)

    def test_an_unknown_path_is_not_a_metrics_body(self) -> None:
        address = self.start()
        status, _, _ = self.scrape(address, "/admin")
        self.assertEqual(status, 404)

    def test_a_scrape_does_not_wait_on_the_worker_lock(self) -> None:
        """The rule: an inference in flight must not delay a scrape, or the reverse."""
        models_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, models_dir, True)
        manager = ModelManager(registry_in(models_dir), models_dir, build_fake_engine, Metrics())
        manager.load("alpha")
        address = self.start(manager)

        holding = threading.Event()
        release = threading.Event()

        def slow_infer(payload):
            holding.set()
            release.wait(timeout=10)
            return Result(text="slow", lines=[])

        manager._engine.infer = slow_infer  # noqa: SLF001 - the lock is the subject
        worker_thread = threading.Thread(target=manager.infer, args=(b"x",), daemon=True)
        worker_thread.start()
        self.assertTrue(holding.wait(timeout=5), "the fake engine never started")

        # The manager's lock is held right now. A scrape must still return promptly.
        started = time.monotonic()
        status, _, body = self.scrape(address)
        elapsed = time.monotonic() - started

        release.set()
        worker_thread.join(timeout=10)

        self.assertEqual(status, 200)
        self.assertLess(elapsed, 2.0, "the scrape waited on the worker lock")
        self.assertIn("dita_worker_model_resident", body)


class MetricsUnderLoadTest(unittest.TestCase):
    """End to end over a real socket: the numbers move because work happened."""

    def test_counters_reflect_a_real_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            models_dir = Path(tmp)
            metrics = Metrics()
            manager = ModelManager(registry_in(models_dir), models_dir, build_fake_engine, metrics)
            worker = fake_worker(write_registry(models_dir))
            socket_path = models_dir / "worker.sock"
            server = SocketServer(worker, manager, socket_path, metrics=metrics)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(thread.join, 5)
            self.addCleanup(server.stop)
            wait_until_listening(socket_path)

            with dip.Requester.connect(socket_path) as client:
                client.handshake()
                client.load("alpha")
                for _ in range(3):
                    client.infer(b"some input")
                client.infer(b"")          # refused: empty payload
                client.load("beta")        # evicts alpha
                client.unload()

            body = parse(metrics.render(worker.name, manager))
            w = f'worker="{worker.name}"'

            self.assertEqual(body[f'dita_worker_ops_total{{{w},op="infer",outcome="ok"}}'], 3)
            self.assertEqual(body[f'dita_worker_ops_total{{{w},op="infer",outcome="error"}}'], 1)
            self.assertEqual(body[f'dita_worker_ops_total{{{w},op="load",outcome="ok"}}'], 2)
            self.assertEqual(body[f'dita_worker_errors_total{{{w},code="bad_request"}}'], 1)
            self.assertEqual(body[f'dita_worker_model_loads_total{{{w},model="alpha"}}'], 1)
            self.assertEqual(body[f'dita_worker_model_evictions_total{{{w}}}'], 1)
            self.assertEqual(body[f'dita_worker_infer_duration_seconds_count{{{w},model="alpha"}}'], 3)
            self.assertGreaterEqual(body[f'dita_worker_connections_total{{{w},outcome="accepted"}}'], 1)
            # unload happened, so nothing is resident at the end.
            self.assertEqual(body[f'dita_worker_model_resident{{{w}}}'], 0)


if __name__ == "__main__":
    unittest.main()
