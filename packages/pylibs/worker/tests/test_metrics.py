"""The metrics surface: the numbers must be real and the scrape must not disturb the work.

A counter that never moves is worse than none, so every assertion here is a before/after
around a real call rather than a check that a name appears in the output.
"""

from __future__ import annotations

import http.client
import os
import shutil
import socket
import socketserver
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import dip

from worker import Metrics, ModelManager, Result, SocketServer, metrics as metrics_mod
from worker.metrics import INFER_BUCKETS

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
        # No model has ever been resident, so the label is empty -- which is how the text
        # format spells a label that is not there.
        self.assertEqual(body['dita_worker_model_resident{worker="inferences-test",model=""}'], 0)
        self.assertEqual(body['dita_worker_model_loading{worker="inferences-test",model=""}'], 0)
        self.assertIn('process_resident_memory_bytes{worker="inferences-test"}', body)
        self.assertGreater(body['process_resident_memory_bytes{worker="inferences-test"}'], 0)

    def test_a_counter_family_is_declared_before_its_first_event(self) -> None:
        """A family that only appears once something has happened reads as a metric nobody
        exports, and both rate() and absent() misbehave against a worker that just started."""
        text = self.metrics.render("inferences-test", None)
        for name in ("dita_worker_ops_total", "dita_worker_errors_total",
                     "dita_worker_model_loads_total", "dita_worker_infer_duration_seconds"):
            self.assertIn(f"# TYPE {name} ", text)

    def test_a_gauge_keeps_naming_the_model_after_it_is_gone(self) -> None:
        """An unload must take the series to zero, not delete it: a label that comes and
        goes with the value is a series nothing can alert on."""
        self.metrics.loaded("alpha", 1.0)
        resident = f'dita_worker_model_resident{{worker="inferences-test",model="alpha"}}'
        seconds = f'dita_worker_model_resident_seconds{{worker="inferences-test",model="alpha"}}'

        class Resident:
            loading = None

            def resident(self):
                return {"id": "alpha", "resident_seconds": 12.5}

        body = parse(self.metrics.render("inferences-test", Resident()))
        self.assertEqual(body[resident], 1)
        self.assertEqual(body[seconds], 12.5)

        body = self.render()  # nothing resident any more
        self.assertEqual(body[resident], 0)
        self.assertEqual(body[seconds], 0)

    def test_a_scrape_cannot_catch_a_histogram_half_observed(self) -> None:
        """The buckets are mutated in place, so a shallow snapshot lets a scrape emit a
        bucket above +Inf and above _count -- nonsense that histogram_quantile believes.

        The observation lands in another thread, between the snapshot and the rendering of
        it: `render` reads the manager after releasing its own lock, so a manager is the
        one seam that is guaranteed to be that moment.
        """
        metrics = self.metrics

        class ObservingManager:
            loading = None

            def resident(self):
                observer = threading.Thread(
                    target=lambda: [metrics.inferred("alpha", 0.01) for _ in range(50)]
                )
                observer.start()
                observer.join(timeout=10)
                return None

        metrics.inferred("alpha", 0.01)
        body = parse(metrics.render("inferences-test", ObservingManager()))

        label = 'worker="inferences-test",model="alpha"'
        count = body[f'dita_worker_infer_duration_seconds_count{{{label}}}']
        self.assertEqual(count, 1, "the snapshot should predate the 50 concurrent observations")
        for edge in INFER_BUCKETS:
            bucket = body[f'dita_worker_infer_duration_seconds_bucket{{{label},le="{edge}"}}']
            self.assertLessEqual(bucket, count, f"le={edge} is above the count")
        self.assertEqual(
            body[f'dita_worker_infer_duration_seconds_bucket{{{label},le="+Inf"}}'], count
        )

    def test_every_counter_moves_on_the_path_it_names(self) -> None:
        cases = [
            ("ops", lambda: self.metrics.op("infer", "ok"),
             'dita_worker_ops_total{worker="inferences-test",op="infer",outcome="ok"}'),
            ("errors", lambda: self.metrics.error("no_model_loaded"),
             'dita_worker_errors_total{worker="inferences-test",code="no_model_loaded"}'),
            ("loads", lambda: self.metrics.loaded("alpha", 1.0),
             'dita_worker_model_loads_total{worker="inferences-test",model="alpha"}'),
            ("evictions", lambda: self.metrics.evicted("alpha"),
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


class ProcessMemoryTest(unittest.TestCase):
    """`process_resident_memory_bytes` is the gauge an eviction is supposed to move."""

    def test_it_reads_the_resident_field_not_the_first_one(self) -> None:
        """statm is `size resident shared ...`: the first field is the address space,
        which is several times the truth and never falls either."""
        with tempfile.NamedTemporaryFile("w", suffix=".statm", delete=False) as handle:
            handle.write("999999 1234 500 10 0 2000 0\n")
        self.addCleanup(Path(handle.name).unlink)

        self.assertEqual(
            metrics_mod.resident_memory_bytes(handle.name), 1234 * metrics_mod.PAGE_SIZE
        )

    REAL = 'process_resident_memory_bytes{worker="inferences-test"}'
    PEAK = 'process_resident_memory_peak_bytes{worker="inferences-test"}'

    def test_both_the_current_size_and_the_peak_are_reported(self) -> None:
        """The peak is worth having; it is not worth having under the name every dashboard
        reads as current memory."""
        body = parse(Metrics().render("inferences-test", None))
        self.assertGreater(body[self.REAL], 0)
        self.assertGreater(body[self.PEAK], 0)

    def test_no_proc_to_read_means_no_gauge_rather_than_a_wrong_one(self) -> None:
        self.assertIsNone(metrics_mod.resident_memory_bytes("/nonexistent/statm"))

        with mock.patch.object(metrics_mod, "resident_memory_bytes", lambda: None):
            body = parse(Metrics().render("inferences-test", None))
        self.assertNotIn(self.REAL, body)
        self.assertGreater(body[self.PEAK], 0, "the peak is still reported")


class AddressTest(unittest.TestCase):
    ADDRESSES = [
        ("ipv4", "127.0.0.1:9109", ("127.0.0.1", 9109), socket.AF_INET),
        ("a hostname", "metrics.internal:80", ("metrics.internal", 80), socket.AF_INET),
        ("bracketed ipv6", "[::1]:9109", ("::1", 9109), socket.AF_INET6),
        ("bracketed ipv6, any", "[::]:9109", ("::", 9109), socket.AF_INET6),
    ]

    def test_an_address_parses_into_a_host_a_port_and_a_family(self) -> None:
        for name, value, expected, family in self.ADDRESSES:
            with self.subTest(name):
                self.assertEqual(metrics_mod.parse_addr(value), expected)
                self.assertEqual(metrics_mod.address_family(expected[0]), family)

    REFUSED = [("no port", "127.0.0.1"), ("no host", ":9109"), ("a named port", "localhost:http"),
               ("empty", "")]

    def test_an_address_that_cannot_be_bound_is_refused_not_guessed(self) -> None:
        for name, value in self.REFUSED:
            with self.subTest(name):
                with self.assertRaises(ValueError):
                    metrics_mod.parse_addr(value)

    def test_the_env_var_decides_whether_there_is_a_port_at_all(self) -> None:
        cases = [("unset", {}, metrics_mod.parse_addr(metrics_mod.DEFAULT_ADDR)),
                 ("off", {"METRICS_ADDR": "off"}, None),
                 ("empty", {"METRICS_ADDR": "  "}, None),
                 ("set", {"METRICS_ADDR": "0.0.0.0:1234"}, ("0.0.0.0", 1234))]
        for name, environment, expected in cases:
            with self.subTest(name):
                with mock.patch.dict(os.environ, environment, clear=True):
                    self.assertEqual(metrics_mod.address_from_env(), expected)


class MetricsHardeningTest(unittest.TestCase):
    """The scrape port is reachable by anything that can reach the pod, and it is HTTP/1.1,
    so a connection outlives its request. Both are bounded on purpose."""

    def start(self, **kwargs):
        self.server = metrics_mod.serve(
            Metrics(), "inferences-test", None, ("127.0.0.1", 0), **kwargs
        )
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        return self.server.server_address

    def connect(self, address) -> socket.socket:
        client = socket.create_connection(address, timeout=5)
        self.addCleanup(client.close)
        return client

    def test_a_client_that_says_nothing_is_hung_up_on(self) -> None:
        client = self.connect(self.start(request_timeout=0.25))
        # Not one byte of a request. Without a handler timeout this read never returns and
        # the thread behind it is pinned for good.
        self.assertEqual(client.recv(1), b"", "the server kept an idle connection open")

    def test_a_scrape_past_the_cap_is_refused_rather_than_queued(self) -> None:
        address = self.start(request_timeout=5.0, max_scrapes=1)

        held = http.client.HTTPConnection(*address, timeout=5)
        self.addCleanup(held.close)
        held.request("GET", "/metrics")
        self.assertEqual(held.getresponse().status, 200)
        # Keep-alive: that connection still holds the one slot there is.

        refused = http.client.HTTPConnection(*address, timeout=5)
        self.addCleanup(refused.close)
        with self.assertLogs("worker.metrics", level="WARNING"):
            refused.request("GET", "/metrics")
            with self.assertRaises((http.client.HTTPException, OSError)):
                refused.getresponse()

        # The cap is a cap, not a leak: closing the held connection frees the slot again.
        held.close()
        for _ in range(200):
            try:
                after = http.client.HTTPConnection(*address, timeout=5)
                after.request("GET", "/metrics")
                status = after.getresponse().status
                after.close()
                break
            except (http.client.HTTPException, OSError):
                time.sleep(0.01)
        else:
            self.fail("the slot was never released")
        self.assertEqual(status, 200)

    def test_a_handler_thread_that_will_not_start_gives_its_slot_back(self) -> None:
        """The slot is taken before the thread and released by the thread, so a thread that
        never runs would shrink the cap permanently -- to nothing, at a cap of one."""
        address = self.start(max_scrapes=1)

        with mock.patch.object(
            socketserver.ThreadingMixIn, "process_request", side_effect=RuntimeError("no thread")
        ):
            with self.assertRaises(RuntimeError):
                self.server.process_request(object(), ("127.0.0.1", 1))

        connection = http.client.HTTPConnection(*address, timeout=5)
        self.addCleanup(connection.close)
        connection.request("GET", "/metrics")
        self.assertEqual(connection.getresponse().status, 200)

    def test_the_server_header_does_not_name_the_interpreter(self) -> None:
        address = self.start()
        connection = http.client.HTTPConnection(*address, timeout=5)
        self.addCleanup(connection.close)
        connection.request("GET", "/metrics")
        header = connection.getresponse().getheader("Server")
        self.assertEqual(header, "worker-metrics")
        self.assertNotIn("Python", header)


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
            # unload happened, so nothing is resident at the end -- and the gauge still
            # names the model it last had, at zero, rather than dropping the series.
            self.assertEqual(body[f'dita_worker_model_resident{{{w},model="beta"}}'], 0)


if __name__ == "__main__":
    unittest.main()
