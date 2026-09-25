"""The service from the outside: the orchestrator's lifecycle over DIP, a TEI client's
requests over HTTP, and the wiring between this service and the framework.

Blackbox first. `LifecycleTest` and `EmbedEndpointTest` touch nothing but a real unix socket
and a real HTTP port; the engine behind them is a real `Embedder` on a fake session.
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
import numpy as np

from embedding_worker import tei
from embedding_worker.__main__ import WORKER
from embedding_worker.engines import ENGINE_NAMES
from embedding_worker.engines.onnx_embedder import EmbedderConfig
from worker import Metrics, ModelManager, SocketServer, load_registry, metrics as metrics_mod

from .support import REGISTRY_YAML, WIDTH, FakeEmbeddingEngine

MANIFEST = Path(__file__).resolve().parent.parent / "models.yaml"


class Harness:
    """One worker, both surfaces: the DIP socket the orchestrator dials and the HTTP port."""

    def __init__(self, test: unittest.TestCase, max_concurrent: int = tei.MAX_CONCURRENT_REQUESTS) -> None:
        FakeEmbeddingEngine.reset()
        test.addCleanup(FakeEmbeddingEngine.reset)
        directory = tempfile.TemporaryDirectory()
        test.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "models.yaml").write_text(REGISTRY_YAML, encoding="utf-8")
        self.manager = ModelManager(load_registry(root / "models.yaml"), root,
                                    lambda name, model_dir, options: FakeEmbeddingEngine(model_dir, options))

        self.socket_path = root / "embedding.sock"
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
    """Model lifetime is the orchestrator's: `/embed` answers only between its load and unload."""

    def test_embed_follows_the_orchestrators_load_and_unload(self) -> None:
        worker = Harness(self)

        status, _, body = worker.http("POST", "/embed", {"inputs": ["alpha"]})
        self.assertEqual((status, body["error_type"]), (503, "Unhealthy"))
        self.assertEqual(worker.http("GET", "/health")[0], 503)
        self.assertIsNone(worker.manager.resident(), "a request must never load a model")

        loaded = worker.dip("load", id="fake-embedder")
        self.assertTrue(loaded["ok"], loaded)
        status, headers, body = worker.http("POST", "/embed", {"inputs": ["alpha"]})
        self.assertEqual(status, 200)
        self.assertEqual(headers["x-model-id"], "fake-embedder")
        self.assertEqual(worker.http("GET", "/health")[0], 200)

        self.assertEqual(worker.dip("unload")["unloaded"], "fake-embedder")
        status, _, body = worker.http("POST", "/embed", {"inputs": ["alpha"]})
        self.assertEqual((status, body["error_type"]), (503, "Unhealthy"))

    def test_dip_infer_points_at_the_http_surface(self) -> None:
        worker = Harness(self)
        worker.dip("load", id="fake-embedder")
        with dip.Requester.connect(worker.socket_path) as requester:
            response = requester.call("infer", b"alpha")
        self.assertEqual(response["error"]["code"], "bad_request")
        self.assertIn("vectors only", response["error"]["message"])


class EmbedEndpointTest(unittest.TestCase):
    def setUp(self) -> None:
        # One slot: a refusal or a failure that forgot to return it makes the next request a 429.
        self.worker = Harness(self, max_concurrent=1)
        self.worker.dip("load", id="fake-embedder")

    def test_a_batch_comes_back_as_one_unit_vector_per_input_in_order(self) -> None:
        texts = ["alpha", "beta beta beta", "gamma", "alpha"]
        status, headers, vectors = self.worker.http("POST", "/embed", {"inputs": texts})

        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual([len(vector) for vector in vectors], [WIDTH] * 4)
        np.testing.assert_allclose(np.linalg.norm(vectors, axis=1), np.ones(4), rtol=1e-6)
        self.assertEqual(vectors[0], vectors[3])
        self.assertNotEqual(vectors[0], vectors[1])
        self.assertNotEqual(vectors[0], vectors[2])

    def test_a_single_string_is_a_batch_of_one(self) -> None:
        _, _, single = self.worker.http("POST", "/embed", {"inputs": "alpha"})
        _, _, batch = self.worker.http("POST", "/embed", {"inputs": ["alpha"]})
        self.assertEqual(single, batch)
        self.assertEqual(len(single), 1)

    def test_the_request_options_change_the_vectors(self) -> None:
        _, _, plain = self.worker.http("POST", "/embed", {"inputs": ["alpha"]})
        cases = [
            ("a prompt", {"prompt_name": "query"}, lambda v: self.assertNotEqual(v, plain)),
            ("fewer dimensions", {"dimensions": 4}, lambda v: self.assertEqual(len(v[0]), 4)),
            ("no normalisation", {"normalize": False},
             lambda v: self.assertNotAlmostEqual(float(np.linalg.norm(v[0])), 1.0, places=3)),
        ]
        for name, options, check in cases:
            with self.subTest(name):
                status, _, vectors = self.worker.http("POST", "/embed", {"inputs": ["alpha"], **options})
                self.assertEqual(status, 200)
                check(vectors)

    def test_each_refusal_carries_teis_status_and_error_type(self) -> None:
        cases = [
            ("empty inputs", {"inputs": []}, 400, "Empty"),
            ("an unknown field", {"inputs": ["alpha"], "model": "x"}, 422, "Validation"),
            ("too many inputs", {"inputs": ["alpha"] * 33}, 422, "Validation"),
            ("an input past the limit", {"inputs": ["beta " * 10]}, 422, "Validation"),
            ("an unknown prompt", {"inputs": ["alpha"], "prompt_name": "passage"}, 422, "Validation"),
            ("dimensions past the model", {"inputs": ["alpha"], "dimensions": WIDTH + 1}, 422, "Validation"),
            ("a body that is not JSON", b"{inputs", 422, "Validation"),
        ]
        for name, body, status, error_type in cases:
            with self.subTest(name):
                got, _, answer = self.worker.http("POST", "/embed", body)
                self.assertEqual((got, answer["error_type"]), (status, error_type))
                self.assertTrue(answer["error"])

    def test_the_wrong_method_is_refused_on_every_route(self) -> None:
        for method, path in (("GET", "/embed"), ("POST", "/info"), ("POST", "/health")):
            with self.subTest(f"{method} {path}"):
                status, headers, _ = self.worker.http(method, path, {} if method == "POST" else None)
                self.assertEqual(status, 405)
                self.assertIn("Allow", headers)

    def test_info_describes_the_resident_model(self) -> None:
        status, _, info = self.worker.http("GET", "/info")
        self.assertEqual(status, 200)
        self.assertEqual(info["model_id"], "fake-embedder")
        self.assertEqual(info["model_type"], {"embedding": {"pooling": "mean"}})
        self.assertEqual((info["max_input_length"], info["max_batch_tokens"]), (6, 12))
        self.assertEqual(info["max_client_batch_size"], tei.MAX_CLIENT_BATCH_SIZE)
        self.assertEqual((info["dimensions"], info["prompt_names"]), (WIDTH, ["query"]))

    def test_an_engine_failure_is_a_backend_error_not_a_validation_one(self) -> None:
        cases = [
            ("the engine raises", {"fail_with": RuntimeError("the graph broke")}, "RuntimeError"),
            ("a value error from inside the graph", {"fail_with": ValueError("shape")}, "ValueError"),
            ("a non-finite vector", {"poison": True}, "non-finite"),
        ]
        for name, hooks, message in cases:
            with self.subTest(name):
                FakeEmbeddingEngine.reset()
                for key, value in hooks.items():
                    setattr(FakeEmbeddingEngine, key, value)
                status, _, answer = self.worker.http("POST", "/embed", {"inputs": ["alpha"]})
                self.assertEqual((status, answer["error_type"]), (424, "Backend"))
                self.assertIn(message, answer["error"])


class OverloadTest(unittest.TestCase):
    # Not a table row: a request held inside the engine while a second one arrives.
    def test_past_the_concurrency_cap_the_answer_is_429_not_a_queue(self) -> None:
        worker = Harness(self, max_concurrent=1)
        worker.dip("load", id="fake-embedder")
        FakeEmbeddingEngine.gate = threading.Event()
        FakeEmbeddingEngine.entered = threading.Event()

        first = []
        holder = threading.Thread(target=lambda: first.append(worker.http("POST", "/embed", {"inputs": ["alpha"]})))
        holder.start()
        self.assertTrue(FakeEmbeddingEngine.entered.wait(timeout=5))

        status, headers, answer = worker.http("POST", "/embed", {"inputs": ["beta"]})
        self.assertEqual((status, answer["error_type"]), (429, "Overloaded"))
        self.assertEqual(headers["Retry-After"], "1")

        FakeEmbeddingEngine.gate.set()
        holder.join(timeout=10)
        self.assertEqual(first[0][0], 200)
        self.assertEqual(worker.http("POST", "/embed", {"inputs": ["beta"]})[0], 200, "the slot was not returned")


class EncodingTest(unittest.TestCase):
    def test_vectors_round_trip_exactly_at_float32(self) -> None:
        vectors = np.random.default_rng(7).standard_normal((3, 64)).astype(np.float32)
        vectors[0, 0] = -0.0
        body = tei.encode_vectors(vectors)
        np.testing.assert_array_equal(np.asarray(json.loads(body), dtype=np.float32), vectors)
        self.assertLess(len(body), len(json.dumps(vectors.tolist(), separators=(",", ":"))))
        self.assertEqual(tei.encode_vectors(np.array([[0.1, -2.5]], dtype=np.float32)), b"[[0.1,-2.5]]")


class ServiceIdentityTest(unittest.TestCase):
    """The wiring: what this service hands the framework, and a manifest the engine can serve."""

    def setUp(self) -> None:
        self.registry = load_registry(MANIFEST)

    def test_what_the_worker_advertises(self) -> None:
        self.assertEqual((WORKER.name, WORKER.prog, WORKER.engines),
                         ("inferences-embedding", "embedding_worker", ENGINE_NAMES))
        self.assertEqual(sorted(WORKER.routes), ["/embed", "/health", "/info"])
        self.assertEqual(WORKER.registry_path, MANIFEST)

    def test_the_default_is_the_model_we_chose(self) -> None:
        # EMB-1, decided after measuring: multilingual rather than English-only, because the bank is
        # English but the second language is not. English-only nomic is still registered below.
        default = self.registry.get(self.registry.default_model)
        self.assertEqual(default.id, "qwen3-embedding-0.6b")
        self.assertEqual(default.langs, ["en", "id", "ja"])
        self.assertEqual(default.options["dimensions"], 1024)
        self.assertIn("Query:", default.options["prompts"]["query"])

    def test_the_english_only_model_says_so(self) -> None:
        # It is not the default any more, but its prefixes are data, not decoration: nomic is
        # asymmetric and degrades badly without them, so a registered entry that loses them is a bug.
        nomic = self.registry.get("nomic-embed-text-v1.5")
        self.assertEqual(nomic.langs, ["en"])
        self.assertIn("English only", nomic.description)
        self.assertEqual(nomic.options["prompts"]["query"], "search_query: ")
        self.assertEqual(nomic.options["prompts"]["document"], "search_document: ")

    def test_the_multilingual_models_cover_indonesian_and_japanese(self) -> None:
        for model_id in ("embeddinggemma-300m", "qwen3-embedding-0.6b"):
            with self.subTest(model_id):
                self.assertTrue({"id", "ja"} <= set(self.registry.get(model_id).langs))

    def test_every_model_is_servable_by_this_engine(self) -> None:
        self.assertEqual(sorted(self.registry.models),
                         ["embeddinggemma-300m", "nomic-embed-text-v1.5", "qwen3-embedding-0.6b"])
        for spec in self.registry.models.values():
            with self.subTest(spec.id):
                self.assertIn(spec.engine, ENGINE_NAMES)
                config = EmbedderConfig.from_options(spec.options)
                self.assertIn(config.onnx, [f.dest for f in spec.files])
                self.assertIn(config.tokenizer, [f.dest for f in spec.files])
                self.assertTrue(all(d < config.dimensions for d in config.matryoshka_dimensions))
                self.assertIn("query", config.prompts)
                self.assertIn("document", config.prompts)

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
