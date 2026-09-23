"""The exact string `--probe` prints, and the exit code beside it.

The table exists because the verdict used to be printed twice -- `readyz: pass pass` -- and
the exit code was correct the whole time, so nothing caught it. These assert the line, byte
for byte. The two scenarios after it run the real CLI against a real worker on a real
socket, which is what the container healthcheck execs.
"""

from __future__ import annotations

import contextlib
import http.client
import io
import os
import signal
import socket
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from worker import ModelManager, Response, SocketServer, fetcher, load_registry
from worker import metrics as metrics_mod
from worker.cli import build_parser, main, probe_line, run_probe

from .support import FakeEngine, build_fake_engine, fake_worker, wait_until_listening, write_registry


class ProbeLineTest(unittest.TestCase):
    CASES = [
        {
            "name": "a passing readyz names the resident model and the uptime",
            "op": "readyz",
            "response": {
                "ok": True,
                "probe": "readyz",
                "status": "pass",
                "uptime_s": 12.345,
                "reasons": [],
                "resident": {"id": "alpha", "engine": "echo"},
                "loading": None,
            },
            "line": "readyz: pass resident=alpha uptime=12.3s",
        },
        {
            "name": "a passing readyz with nothing loaded says so",
            "op": "readyz",
            "response": {
                "ok": True, "status": "pass", "uptime_s": 2.0, "reasons": [],
                "resident": None, "loading": None,
            },
            "line": "readyz: pass resident=none uptime=2.0s",
        },
        {
            "name": "a passing readyz during a load names the incoming model",
            "op": "readyz",
            "response": {
                "ok": True, "status": "pass", "uptime_s": 5.06, "reasons": [],
                "resident": None, "loading": "beta",
            },
            "line": "readyz: pass resident=none loading=beta uptime=5.1s",
        },
        {
            "name": "livez carries no resident field",
            "op": "livez",
            "response": {"ok": True, "status": "pass", "uptime_s": 41.24, "reasons": []},
            "line": "livez: pass uptime=41.2s",
        },
        {
            "name": "startupz carries no resident field",
            "op": "startupz",
            "response": {"ok": True, "status": "pass", "uptime_s": 41.24, "reasons": []},
            "line": "startupz: pass uptime=41.2s",
        },
        {
            "name": "a failing probe prints the reason",
            "op": "readyz",
            "response": {
                "ok": False, "status": "fail", "uptime_s": 0.5,
                "reasons": ["the models directory /models is not writable"],
                "resident": None, "loading": None,
            },
            "line": "readyz: fail the models directory /models is not writable",
        },
        {
            "name": "several reasons are joined",
            "op": "readyz",
            "response": {
                "ok": False, "status": "fail", "uptime_s": 0.5,
                "reasons": ["first thing", "second thing"],
            },
            "line": "readyz: fail first thing; second thing",
        },
        {
            "name": "a failure with no reasons still says something",
            "op": "livez",
            "response": {"ok": False, "status": "fail", "reasons": []},
            "line": "livez: fail no reason given",
        },
        {
            "name": "a missing uptime is omitted rather than printed as None",
            "op": "livez",
            "response": {"ok": True, "status": "pass", "reasons": []},
            "line": "livez: pass",
        },
    ]

    def test_the_printed_line(self) -> None:
        for case in self.CASES:
            with self.subTest(case["name"]):
                self.assertEqual(probe_line(case["op"], case["response"]), case["line"])

    def test_the_verdict_is_never_printed_twice(self) -> None:
        """The actual regression: `status` holds the same word as the verdict."""
        for case in self.CASES:
            with self.subTest(case["name"]):
                line = case["line"]
                verdict = "pass" if case["response"]["ok"] else "fail"
                self.assertNotIn("pass pass", line)
                self.assertNotIn("fail fail", line)
                self.assertEqual(line.split().count(verdict), 1, line)


class ProbeCLITest(unittest.TestCase):
    """`--probe` end to end: a real socket, a real server, the real printed line."""

    def setUp(self) -> None:
        FakeEngine.built.clear()
        self._real_ensure = fetcher.ensure_model
        self.addCleanup(setattr, fetcher, "ensure_model", self._real_ensure)
        fetcher.ensure_model = lambda models_dir, spec, *_: []

    def start_worker(self, models_dir: Path) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "worker.sock"
        registry_path = write_registry(Path(directory.name))

        manager = ModelManager(load_registry(registry_path), models_dir, build_fake_engine)
        server = SocketServer(
            fake_worker(registry_path), manager, path, idle_timeout=10.0, message_timeout=5.0
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        # Cleanups run last-registered-first: stop the accept loop, then join it.
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.stop)
        wait_until_listening(path)
        return path

    def probe(self, path: Path, name: str) -> tuple:
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            code = run_probe(path, name)
        return code, captured.getvalue().strip()

    def test_a_passing_probe_prints_one_verdict_and_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory() as models:
            path = self.start_worker(Path(models))

            code, line = self.probe(path, "ready")
            self.assertEqual(code, 0)
            self.assertNotIn("pass pass", line)
            self.assertRegex(line, r"^readyz: pass resident=none uptime=\d+\.\d+s$")

            code, line = self.probe(path, "live")
            self.assertEqual(code, 0)
            self.assertRegex(line, r"^livez: pass uptime=\d+\.\d+s$")

            code, line = self.probe(path, "startup")
            self.assertEqual(code, 0)
            self.assertRegex(line, r"^startupz: pass uptime=\d+\.\d+s$")

    def test_a_failing_probe_prints_the_reason_and_exits_one(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            models = Path(parent) / "readonly"
            models.mkdir()
            models.chmod(0o500)
            try:
                path = self.start_worker(models)
                code, line = self.probe(path, "ready")

                self.assertEqual(code, 1)
                self.assertEqual(line, f"readyz: fail the models directory {models} is not writable")

                # Liveness is deliberately indifferent to a broken dependency.
                code, line = self.probe(path, "live")
                self.assertEqual(code, 0)
                self.assertRegex(line, r"^livez: pass uptime=\d+\.\d+s$")
            finally:
                # Restore before the TemporaryDirectory tries to remove it.
                models.chmod(0o700)


class FlagDefaultsTest(unittest.TestCase):
    """What an operator sets, and where each default comes from.

    A table: one row per flag, because the interesting part is the *source* of the value --
    the worker, the environment, or the command line -- and that is one line per case.
    """

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.registry_path = write_registry(self.root)
        self.worker = fake_worker(self.registry_path)

    def cases(self) -> list:
        return [
            ("the socket is derived from the service name", {}, [], "socket",
             "/run/dita/inferences-fake.sock"),
            ("SOCKET_PATH wins over the derived default", {"SOCKET_PATH": "/tmp/w.sock"}, [],
             "socket", "/tmp/w.sock"),
            ("--socket wins over the environment", {"SOCKET_PATH": "/tmp/w.sock"},
             ["--socket", "/tmp/flag.sock"], "socket", "/tmp/flag.sock"),
            ("the registry defaults to the one the service ships", {}, [], "registry",
             str(self.registry_path)),
            ("MODELS_REGISTRY points somewhere else", {"MODELS_REGISTRY": "/etc/models.yaml"},
             [], "registry", "/etc/models.yaml"),
            ("the models directory is the same for every worker", {}, [], "models_dir",
             "/models"),
            ("MODELS_DIR overrides it", {"MODELS_DIR": "/data/models"}, [], "models_dir",
             "/data/models"),
            ("nothing is preloaded unless asked", {}, [], "preload", None),
            ("PRELOAD_MODEL asks for one", {"PRELOAD_MODEL": "beta"}, [], "preload", "beta"),
            ("the log level env var is the service's own", {"OTHER_LOG_LEVEL": "debug"}, [],
             "log_level", "debug"),
        ]

    def test_where_each_default_comes_from(self) -> None:
        worker = replace(self.worker, log_level_env="OTHER_LOG_LEVEL")
        for name, environment, argv, attribute, expected in self.cases():
            with self.subTest(name):
                with mock.patch.dict(os.environ, environment, clear=True):
                    args = build_parser(worker).parse_args(argv)
                self.assertEqual(getattr(args, attribute), expected)


class MainTest(unittest.TestCase):
    """`main` as the process sees it: one probe, or a reason and an exit code."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.worker = fake_worker(write_registry(self.root))
        # main installs SIGTERM/SIGINT handlers in this process; put them back afterwards.
        for signum in (signal.SIGTERM, signal.SIGINT):
            self.addCleanup(signal.signal, signum, signal.getsignal(signum))
        # It also configures the root logger, which would then narrate every later test.
        configured = mock.patch("logging.basicConfig")
        configured.start()
        self.addCleanup(configured.stop)
        # An ephemeral port, never the fixed default: a test that binds 9109 takes it from
        # whatever else on this machine wants it, including a worker someone is running.
        metrics_port = mock.patch.dict(os.environ, {"METRICS_ADDR": "127.0.0.1:0"})
        metrics_port.start()
        self.addCleanup(metrics_port.stop)

    def test_a_registry_that_will_not_parse_exits_two(self) -> None:
        with self.assertLogs("worker.cli", level="ERROR") as logged:
            code = main(self.worker, ["--registry", str(self.root / "absent.yaml")])
        self.assertEqual(code, 2)
        self.assertIn("not found", logged.output[0])

    def test_a_metrics_address_that_cannot_be_parsed_is_a_reason_not_a_traceback(self) -> None:
        with mock.patch.dict(os.environ, {"METRICS_ADDR": "127.0.0.1"}):
            with self.assertLogs("worker.cli", level="ERROR") as logged:
                code = main(self.worker, ["--models-dir", str(self.root / "models")])
        self.assertEqual(code, 2)
        self.assertIn("must be host:port", logged.output[0])

    def test_the_metrics_port_does_not_outlive_main(self) -> None:
        """`main` starts the metrics server before the socket server, so any exit after
        that point used to leave the port bound for the life of the process."""
        locked = self.root / "locked"
        locked.mkdir()
        locked.chmod(0o500)
        self.addCleanup(locked.chmod, 0o700)

        started = []
        real_serve = metrics_mod.serve

        def remember(*args, **kwargs):
            server = real_serve(*args, **kwargs)
            started.append(server)
            return server

        with mock.patch.object(metrics_mod, "serve", remember):
            with self.assertLogs("worker.cli", level="ERROR"):
                code = main(self.worker, ["--socket", str(locked / "sub" / "w.sock"),
                                          "--models-dir", str(self.root / "models")])

        self.assertEqual(code, 3)
        self.assertEqual(len(started), 1, "main did not start a metrics server")
        # Bindable again, which is only true because main gave the port back.
        with socket.create_server(started[0].server_address[:2]) as rebound:
            self.assertEqual(rebound.getsockname()[1], started[0].server_address[1])

    def test_the_routes_a_worker_names_are_served_by_the_port_main_starts(self) -> None:
        locked = self.root / "locked"
        locked.mkdir()
        locked.chmod(0o500)
        self.addCleanup(locked.chmod, 0o700)
        worker = replace(
            self.worker, routes={"/hello": lambda manager, method, body: Response(200, b"hi")}
        )

        answers = []
        real_serve = metrics_mod.serve

        def ask(*args, **kwargs):
            server = real_serve(*args, **kwargs)
            conn = http.client.HTTPConnection(*server.server_address[:2], timeout=5)
            conn.request("GET", "/hello")
            response = conn.getresponse()
            answers.append((response.status, response.read()))
            conn.close()
            return server

        with mock.patch.object(metrics_mod, "serve", ask):
            with self.assertLogs("worker.cli", level="ERROR"):
                main(worker, ["--socket", str(locked / "sub" / "w.sock"),
                              "--models-dir", str(self.root / "models")])

        self.assertEqual(answers, [(200, b"hi")])

    def test_a_socket_directory_it_cannot_use_exits_three(self) -> None:
        """The docker-volume-owned-by-root failure: a reason, not a traceback."""
        readonly = self.root / "readonly"
        readonly.mkdir()
        readonly.chmod(0o500)
        self.addCleanup(readonly.chmod, 0o700)

        with self.assertLogs("worker.cli", level="ERROR") as logged:
            code = main(
                self.worker,
                [
                    "--socket", str(readonly / "sub" / "worker.sock"),
                    "--models-dir", str(self.root / "models"),
                ],
            )
        self.assertEqual(code, 3)
        self.assertIn("cannot use the socket directory", logged.output[0])
