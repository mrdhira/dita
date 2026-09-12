"""`dispatch` is the whole request surface; the socket server is a thin wrapper over it.

The table is the refusals -- one row per malformed request, so a failure names the row. The
socket scenarios below are not a table: each owns a listener, a thread and, in one case,
sixteen live connections.
"""

from __future__ import annotations

import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path

import dip
from dita_worker import MAX_CONNECTIONS, ModelManager, SocketServer, dispatch, fetcher, load_registry

from .support import FakeEngine, build_fake_engine, fake_worker, wait_until_listening, write_registry


class DispatchTest(unittest.TestCase):
    def setUp(self) -> None:
        FakeEngine.built.clear()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        registry_path = write_registry(Path(directory.name))
        self.worker = fake_worker(registry_path)
        self.manager = ModelManager(
            load_registry(registry_path), Path("/nonexistent"), build_fake_engine
        )

        self._real_ensure = fetcher.ensure_model
        self.addCleanup(setattr, fetcher, "ensure_model", self._real_ensure)
        fetcher.ensure_model = lambda models_dir, spec, *_: []

    def test_handshake_advertises_the_limits_a_peer_needs(self) -> None:
        response = dispatch(self.worker, self.manager, {"op": "handshake"}, b"")
        self.assertTrue(response["ok"])
        self.assertEqual(response["protocol"], dip.PROTOCOL_VERSION)
        for field in ("max_chunk", "max_control", "max_payload"):
            self.assertIn(field, response["limits"])
        self.assertEqual(response["limits"]["max_chunk"], dip.MAX_CHUNK)
        self.assertIsNone(response["resident"])

    def test_handshake_advertises_the_identity_the_service_supplied(self) -> None:
        """The seam: nothing in this package names a service, a version or an engine."""
        response = dispatch(self.worker, self.manager, {"op": "handshake"}, b"")
        self.assertEqual(response["service"], "inferences-fake")
        self.assertEqual(response["version"], "9.9.9")
        self.assertEqual(response["engines"], ["echo", "reverse", "upper"])
        self.assertEqual(response["default_model"], "alpha")

    def test_load_accepts_model_as_an_alias_for_id(self) -> None:
        """`model` is meaningful on load and meaningless on infer; only load takes it."""
        response = dispatch(self.worker, self.manager, {"op": "load", "model": "gamma"}, b"")
        self.assertTrue(response["ok"])
        self.assertEqual(response["id"], "gamma")

    def test_list_then_load_then_unload(self) -> None:
        listing = dispatch(self.worker, self.manager, {"op": "list"}, b"")
        self.assertTrue(listing["ok"])
        self.assertEqual(listing["default_model"], "alpha")
        self.assertIn("alpha", [m["id"] for m in listing["models"]])

        loaded = dispatch(self.worker, self.manager, {"op": "load", "id": "alpha"}, b"")
        self.assertTrue(loaded["ok"])
        self.assertIsNone(loaded["unloaded"])

        self.assertEqual(
            dispatch(self.worker, self.manager, {"op": "handshake"}, b"")["resident"]["id"], "alpha"
        )
        self.assertEqual(
            dispatch(self.worker, self.manager, {"op": "unload"}, b"")["unloaded"], "alpha"
        )

    BAD_REQUESTS = [
        ("load with no id", {"op": "load"}, b"", "bad_request", "`id`"),
        ("an op that does not exist", {"op": "teleport"}, b"", "bad_request", "teleport"),
        ("no op at all", {}, b"", "bad_request", "None"),
        ("an op of the wrong type", {"op": 7}, b"", "bad_request", "7"),
        # An empty payload is rejected before the resident-model check, so this is
        # bad_request rather than no_model_loaded.
        ("infer with an empty payload", {"op": "infer"}, b"", "bad_request", "message payload"),
        ("infer with bytes but nothing loaded", {"op": "infer"}, b"png",
         "no_model_loaded", "load"),
        ("load of a model the registry does not have", {"op": "load", "id": "nope"},
         b"", "unknown_model", "nope"),
        # A field the op does not take is refused, never ignored: a caller who sends
        # `model` to infer has a different model in mind than the resident one.
        ("infer carrying a model field", {"op": "infer", "model": "beta"}, b"png",
         "bad_request", "`load` the model first"),
        ("an op with a field it does not take", {"op": "list", "verbose": True}, b"",
         "bad_request", "'verbose'"),
    ]

    def test_malformed_requests_get_a_coded_error(self) -> None:
        for name, control, payload, code, fragment in self.BAD_REQUESTS:
            with self.subTest(name):
                response = dispatch(self.worker, self.manager, control, payload)
                self.assertFalse(response["ok"])
                self.assertEqual(response["error"]["code"], code)
                self.assertIn(fragment, response["error"]["message"])


class SocketServerTest(unittest.TestCase):
    """The real server over a real unix socket, with a stub manager in place of engines."""

    LINES = 4000

    class StubManager:
        """Enough of ModelManager for dispatch, returning a deliberately huge result."""

        def __init__(self, registry, line_count: int, models_dir: Path) -> None:
            self.registry = registry
            self.loading = None
            self.models_dir = models_dir
            self.last_error = None
            self._line_count = line_count

        def list(self):
            return {"models": [], "default_model": self.registry.default_model, "resident": None}

        def resident(self):
            return None

        def infer(self, payload: bytes):
            line = {
                "text": "日本語のテキスト認識",
                "confidence": 0.99961,
                "box": [[39.0, 193.0], [507.0, 193.0], [507.0, 254.0], [39.0, 254.0]],
            }
            return {"text": "x", "lines": [line] * self._line_count, "model": "stub", "infer_ms": 1.0}

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "worker.sock"
        registry_path = write_registry(Path(self.directory.name))

        manager = self.StubManager(
            load_registry(registry_path), self.LINES, Path(self.directory.name)
        )
        server = SocketServer(
            fake_worker(registry_path), manager, self.path, idle_timeout=10.0, message_timeout=0.5
        )
        self.server = server
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        # Cleanups run last-registered-first: stop the accept loop, then join it.
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.stop)
        wait_until_listening(self.path)

    def connect(self) -> socket.socket:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        sock.settimeout(10)
        sock.connect(str(self.path))
        self.addCleanup(sock.close)
        return sock

    def test_a_response_far_over_so_sndbuf_arrives_intact(self) -> None:
        """End to end over a real socket: before the framing fix this closed the connection."""
        sock = self.connect()
        dip.send_message(sock, {"op": "infer"}, b"pretend png bytes")
        response, _payload = dip.recv_message(sock)

        size = len(json.dumps(response, ensure_ascii=False).encode("utf-8"))
        self.assertGreater(size, sock.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF))
        self.assertTrue(response["ok"])
        self.assertEqual(len(response["lines"]), self.LINES)

    def test_a_stalled_peer_is_timed_out_with_an_error_not_a_reset(self) -> None:
        sock = self.connect()
        control = json.dumps({"op": "infer"}).encode("utf-8")
        sock.send(
            json.dumps(
                {
                    "protocol": dip.PROTOCOL_VERSION,
                    "control_len": len(control),
                    "payload_len": 4 * dip.MAX_CHUNK,
                }
            ).encode("utf-8")
        )
        sock.send(control)
        sock.send(b"z" * dip.MAX_CHUNK)  # one of four announced chunks, then nothing

        response, _payload = dip.recv_message(sock)
        self.assertEqual(response["error"]["code"], "timeout")

    def test_the_probe_ops_answer_over_the_socket(self) -> None:
        """What the container healthcheck execs has to work on the real transport."""
        sock = self.connect()
        for op in ("livez", "readyz", "startupz"):
            dip.send_message(sock, {"op": op})
            response, _payload = dip.recv_message(sock)
            self.assertTrue(response["ok"], f"{op}: {response.get('reasons')}")
            self.assertEqual(response["probe"], op)

    # Not a table row: it holds 16 live sockets open at once.
    def test_connections_past_the_cap_are_told_they_are_refused(self) -> None:
        held = [self.connect() for _ in range(MAX_CONNECTIONS)]
        for sock in held:
            dip.send_message(sock, {"op": "handshake"})
            self.assertTrue(dip.recv_message(sock)[0]["ok"])

        extra = self.connect()
        response, _payload = dip.recv_message(extra)
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "busy")
