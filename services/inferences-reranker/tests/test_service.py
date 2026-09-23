"""The service from the outside: the orchestrator's lifecycle over DIP, a TEI client's
requests over HTTP, and the wiring between this service and the framework.

Blackbox first: nothing here reaches past a real unix socket and a real HTTP port; behind them
is a real `Reranker` on a fake graph.
"""

from __future__ import annotations

import http.client
import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

import dip
from textinfer import InvalidRequest

from reranker_worker import tei
from reranker_worker.__main__ import WORKER
from reranker_worker.engines import ENGINE_NAMES
from reranker_worker.engines.cross_encoder import RerankerConfig
from worker import Metrics, ModelManager, SocketServer, load_registry, metrics as metrics_mod

from .support import REGISTRY_YAML, FakeRerankerEngine

MANIFEST = Path(__file__).resolve().parent.parent / "models.yaml"
HINDSIGHT = {"query": "mars", "texts": ["noise", "relevant relevant", "red"], "return_text": False}


class Harness:
    """One worker, both surfaces: the DIP socket the orchestrator dials and the HTTP port."""

    def __init__(self, test: unittest.TestCase, max_concurrent: int = tei.MAX_CONCURRENT_REQUESTS) -> None:
        FakeRerankerEngine.reset()
        test.addCleanup(FakeRerankerEngine.reset)
        directory = tempfile.TemporaryDirectory()
        test.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "models.yaml").write_text(REGISTRY_YAML, encoding="utf-8")
        self.manager = ModelManager(load_registry(root / "models.yaml"), root,
                                    lambda name, model_dir, options: FakeRerankerEngine(model_dir, options))
        self.socket_path = root / "reranker.sock"
        server = SocketServer(WORKER, self.manager, self.socket_path)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        test.addCleanup(thread.join, 5)
        test.addCleanup(server.stop)
        _wait_until_listening(self.socket_path)
        http_server = metrics_mod.serve(Metrics(), WORKER.name, self.manager, ("127.0.0.1", 0),
                                        routes=tei.routes(max_concurrent))
        test.addCleanup(http_server.server_close)
        test.addCleanup(http_server.shutdown)
        self.address = http_server.server_address[:2]

    def dip(self, op: str, **fields):
        with dip.Requester.connect(self.socket_path) as requester:
            return requester.call(op, **fields)

    def http(self, method: str, path: str, body=None):
        conn = http.client.HTTPConnection(*self.address, timeout=10)
        try:
            payload = None if body is None else (body if isinstance(body, bytes) else json.dumps(body))
            conn.request(method, path, body=payload, headers={"Content-Type": "application/json"})
            response = conn.getresponse()
            raw = response.read()
            return response.status, dict(response.getheaders()), (json.loads(raw) if raw else None)
        finally:
            conn.close()


class LifecycleTest(unittest.TestCase):
    """Model lifetime is the orchestrator's: `/rerank` answers only between its load and unload."""

    def test_rerank_follows_the_orchestrators_load_and_unload(self) -> None:
        worker = Harness(self)
        status, _, body = worker.http("POST", "/rerank", HINDSIGHT)
        self.assertEqual((status, body["error_type"]), (503, "Unhealthy"))
        self.assertEqual(worker.http("GET", "/health")[0], 503)
        self.assertEqual(worker.http("GET", "/info")[0], 503)
        self.assertIsNone(worker.manager.resident(), "a request must never load a model")

        self.assertTrue(worker.dip("load", id="fake-reranker")["ok"])
        status, headers, ranks = worker.http("POST", "/rerank", HINDSIGHT)
        self.assertEqual(status, 200)
        self.assertEqual(headers["x-model-id"], "fake-reranker")
        self.assertEqual([rank["index"] for rank in ranks], [1, 2, 0])

        self.assertEqual(worker.dip("unload")["unloaded"], "fake-reranker")
        status, _, body = worker.http("POST", "/rerank", HINDSIGHT)
        self.assertEqual((status, body["error_type"]), (503, "Unhealthy"))

    def test_dip_infer_points_at_the_http_surface(self) -> None:
        worker = Harness(self)
        worker.dip("load", id="fake-reranker")
        with dip.Requester.connect(worker.socket_path) as requester:
            response = requester.call("infer", b"pair")
        self.assertEqual(response["error"]["code"], "bad_request")
        self.assertIn("scores only", response["error"]["message"])


class RerankEndpointTest(unittest.TestCase):
    def setUp(self) -> None:
        # One slot: a refusal or a failure that forgot to return it makes the next request a 429.
        self.worker = Harness(self, max_concurrent=1)
        self.worker.dip("load", id="fake-reranker")

    def test_hindsights_request_gets_every_index_once_best_first(self) -> None:
        status, headers, ranks = self.worker.http("POST", "/rerank", HINDSIGHT)
        self.assertEqual((status, headers["Content-Type"]), (200, "application/json"))
        self.assertEqual(sorted(rank["index"] for rank in ranks), [0, 1, 2])
        self.assertEqual([sorted(rank) for rank in ranks], [["index", "score"]] * 3)
        scores = [rank["score"] for rank in ranks]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertTrue(all(0.0 < score < 1.0 for score in scores))

    def test_an_over_long_memory_is_cut_by_default_and_refused_on_request(self) -> None:
        long = {"query": "mars", "texts": ["relevant " * 20, "noise"]}
        status, _, ranks = self.worker.http("POST", "/rerank", long)
        self.assertEqual(status, 200, "auto_truncate should have cut it")
        self.assertEqual(ranks[0]["index"], 0)
        status, _, body = self.worker.http("POST", "/rerank", {**long, "truncate": False})
        self.assertEqual((status, body["error_type"]), (422, "Validation"))

    def test_each_refusal_carries_teis_status_and_error_type(self) -> None:
        cases = [
            ("empty texts", {"query": "q", "texts": []}, 400, "Empty"),
            ("an unknown field", {"query": "q", "texts": ["a"], "model": "x"}, 422, "Validation"),
            ("too many texts", {"query": "q", "texts": ["a"] * 33}, 422, "Validation"),
            ("a query with no room left", {"query": "mars red planet it find", "texts": ["a"]}, 422, "Validation"),
            ("a body that is not JSON", b"{query", 422, "Validation"),
            ("a body that is not an object", b"[]", 422, "Validation"),
        ]
        for name, body, status, error_type in cases:
            with self.subTest(name):
                got, _, answer = self.worker.http("POST", "/rerank", body)
                self.assertEqual((got, answer["error_type"]), (status, error_type))
                self.assertTrue(answer["error"])

    def test_the_wrong_method_is_refused_on_every_route(self) -> None:
        for method, path in (("GET", "/rerank"), ("POST", "/info"), ("POST", "/health")):
            with self.subTest(f"{method} {path}"):
                status, headers, _ = self.worker.http(method, path, {} if method == "POST" else None)
                self.assertEqual(status, 405)
                self.assertIn("Allow", headers)

    def test_info_describes_a_reranker(self) -> None:
        status, _, info = self.worker.http("GET", "/info")
        self.assertEqual(status, 200)
        self.assertEqual(info["model_id"], "fake-reranker")
        self.assertIn("reranker", info["model_type"])
        self.assertEqual((info["max_input_length"], info["max_batch_tokens"]), (12, 24))
        self.assertEqual((info["max_client_batch_size"], info["auto_truncate"]), (32, True))

    def test_an_engine_failure_is_a_backend_error_not_a_validation_one(self) -> None:
        cases = [
            ("the engine raises", {"fail_with": RuntimeError("the graph broke")}, 424, "Backend", "RuntimeError"),
            ("a value error inside the graph", {"fail_with": ValueError("shape")}, 424, "Backend", "ValueError"),
            ("a NaN score", {"poison": True}, 424, "Backend", "score is NaN"),
            ("the caller's mistake", {"fail_with": InvalidRequest("no")}, 422, "Validation", "no"),
        ]
        for name, hooks, status, error_type, message in cases:
            with self.subTest(name):
                FakeRerankerEngine.reset()
                for key, value in hooks.items():
                    setattr(FakeRerankerEngine, key, value)
                got, _, answer = self.worker.http("POST", "/rerank", HINDSIGHT)
                self.assertEqual((got, answer["error_type"]), (status, error_type))
                self.assertIn(message, answer["error"])


class OverloadTest(unittest.TestCase):
    # Not a table row: a request held inside the engine while a second one arrives.
    def test_past_the_concurrency_cap_the_answer_is_429_not_a_queue(self) -> None:
        worker = Harness(self, max_concurrent=1)
        worker.dip("load", id="fake-reranker")
        FakeRerankerEngine.gate = threading.Event()
        FakeRerankerEngine.entered = threading.Event()
        first = []
        holder = threading.Thread(target=lambda: first.append(worker.http("POST", "/rerank", HINDSIGHT)))
        holder.start()
        self.assertTrue(FakeRerankerEngine.entered.wait(timeout=5))

        status, headers, answer = worker.http("POST", "/rerank", HINDSIGHT)
        self.assertEqual((status, answer["error_type"], headers["Retry-After"]), (429, "Overloaded", "1"))

        FakeRerankerEngine.gate.set()
        holder.join(timeout=10)
        self.assertEqual(first[0][0], 200)
        self.assertEqual(worker.http("POST", "/rerank", HINDSIGHT)[0], 200, "the slot was not returned")


class ServiceIdentityTest(unittest.TestCase):
    """The wiring: what this service hands the framework, and a manifest the engine can serve."""

    def setUp(self) -> None:
        self.registry = load_registry(MANIFEST)

    def test_what_the_worker_advertises(self) -> None:
        self.assertEqual((WORKER.name, WORKER.prog, WORKER.engines),
                         ("inferences-reranker", "reranker_worker", ENGINE_NAMES))
        self.assertEqual(sorted(WORKER.routes), ["/health", "/info", "/rerank"])
        self.assertEqual(WORKER.registry_path, MANIFEST)

    def test_the_one_model_is_the_multilingual_qwen3_reranker(self) -> None:
        self.assertEqual(list(self.registry.models), ["qwen3-reranker-0.6b"])
        spec = self.registry.get(self.registry.default_model)
        self.assertEqual(spec.engine, "onnx_cross_encoder")
        self.assertTrue({"en", "id", "ja"} <= set(spec.langs))
        config = RerankerConfig.from_options(spec.options)
        self.assertTrue(config.auto_truncate)
        self.assertIn(config.onnx, [f.dest for f in spec.files])
        self.assertIn(config.tokenizer, [f.dest for f in spec.files])

    def test_the_tokenizer_comes_from_the_official_repository(self) -> None:
        (tokenizer,) = [f for f in self.registry.get("qwen3-reranker-0.6b").files if f.dest == "tokenizer.json"]
        self.assertEqual(tokenizer.repo, "Qwen/Qwen3-Reranker-0.6B")

    def test_every_file_is_pinned(self) -> None:
        for spec in self.registry.models.values():
            for pinned in spec.files:
                with self.subTest(f"{spec.id}/{pinned.dest}"):
                    self.assertRegex(pinned.sha256 or "", r"^[0-9a-f]{64}$")
                    self.assertRegex(pinned.revision, r"^[0-9a-f]{40}$")
                    self.assertGreater(pinned.bytes or 0, 0)


def _wait_until_listening(path: Path, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as probe:
                probe.connect(str(path))
                return
        except OSError:
            time.sleep(0.02)
    raise AssertionError(f"no server listening at {path}")


if __name__ == "__main__":
    unittest.main()
