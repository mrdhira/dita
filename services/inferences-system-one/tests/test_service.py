"""The service from the outside: the orchestrator's lifecycle over DIP, its /decide over HTTP,
and the wiring between this service and the framework.

Blackbox first: nothing here reaches past a real unix socket and a real HTTP port; behind them
is a real `Decider` on a fake graph.
"""

from __future__ import annotations

import hashlib
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
from textinfer import InvalidRequest

from system_one_worker import decide
from system_one_worker.__main__ import WORKER
from system_one_worker.engines import ENGINE_NAMES
from system_one_worker.engines.decision import confidence_from_probs
from worker import Metrics, ModelManager, SocketServer, load_registry, metrics as metrics_mod

from .support import FakeDecisionEngine

MANIFEST = Path(__file__).resolve().parent.parent / "models.yaml"
REVISION = "0123456789abcdef0123456789abcdef01234567"
ALERT = {"text": "oom restart restart",
         "questions": [{"name": "severity", "type": "choice", "options": ["info", "warning", "critical"]},
                       {"name": "needs_human", "type": "noul", "options": ["false", "true"]}]}


class Harness:
    """One worker, both surfaces: the DIP socket the orchestrator dials and the HTTP port.
    The model's one file is already on disk with its pinned digest, so `load` goes through the
    real fetcher without a network."""

    def __init__(self, test: unittest.TestCase, max_concurrent: int = decide.MAX_CONCURRENT_REQUESTS,
                 **route_options) -> None:
        FakeDecisionEngine.reset()
        test.addCleanup(FakeDecisionEngine.reset)
        directory = tempfile.TemporaryDirectory()
        test.addCleanup(directory.cleanup)
        root = Path(directory.name)
        blob = b"fake weights"
        (root / "fake-decider").mkdir()
        (root / "fake-decider" / "weights.bin").write_bytes(blob)
        (root / "models.yaml").write_text(f"""\
version: 1
default_model: fake-decider
models:
  - id: fake-decider
    description: an in-memory decision model
    engine: onnx_decision
    langs: [multilingual]
    source: {{type: huggingface, repo: example/fake, revision: {REVISION}}}
    files:
      - {{path: weights.bin, sha256: {hashlib.sha256(blob).hexdigest()}, bytes: {len(blob)}}}
""", encoding="utf-8")
        self.manager = ModelManager(load_registry(root / "models.yaml"), root,
                                    lambda name, model_dir, options: FakeDecisionEngine(model_dir, options))
        self.socket_path = root / "system-one.sock"
        server = SocketServer(WORKER, self.manager, self.socket_path)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        test.addCleanup(thread.join, 5)
        test.addCleanup(server.stop)
        _wait_until_listening(self.socket_path)
        http_server = metrics_mod.serve(Metrics(), WORKER.name, self.manager, ("127.0.0.1", 0),
                                        routes=decide.routes(max_concurrent, **route_options))
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
            content_type = response.getheader("Content-Type", "")
            parsed = json.loads(raw) if raw and content_type.startswith("application/json") else raw
            return response.status, dict(response.getheaders()), parsed
        finally:
            conn.close()


class LifecycleTest(unittest.TestCase):
    """Model lifetime is the orchestrator's: `/decide` answers only between its load and unload."""

    def test_decide_follows_the_orchestrators_load_and_unload(self) -> None:
        worker = Harness(self)
        status, _, body = worker.http("POST", "/decide", ALERT)
        self.assertEqual((status, body["error_type"]), (503, "Unhealthy"))
        self.assertEqual(worker.http("GET", "/health")[0], 503)
        self.assertEqual(worker.http("GET", "/info")[0], 503)
        self.assertIsNone(worker.manager.resident(), "a request must never load a model")

        self.assertTrue(worker.dip("load", id="fake-decider")["ok"])
        status, headers, answer = worker.http("POST", "/decide", ALERT)
        self.assertEqual(status, 200)
        self.assertEqual(headers["x-model-id"], "fake-decider")
        self.assertEqual(worker.http("GET", "/health")[0], 200)

        self.assertEqual(worker.dip("unload")["unloaded"], "fake-decider")
        status, _, body = worker.http("POST", "/decide", ALERT)
        self.assertEqual((status, body["error_type"]), (503, "Unhealthy"))

    def test_dip_infer_points_at_the_http_surface(self) -> None:
        worker = Harness(self)
        worker.dip("load", id="fake-decider")
        with dip.Requester.connect(worker.socket_path) as requester:
            response = requester.call("infer", b"text")
        self.assertEqual(response["error"]["code"], "bad_request")
        self.assertIn("POST /decide on the HTTP port", response["error"]["message"])


class DecideEndpointTest(unittest.TestCase):
    def setUp(self) -> None:
        # One slot: a refusal or a failure that forgot to return it makes the next request a 429.
        self.worker = Harness(self, max_concurrent=1)
        self.worker.dip("load", id="fake-decider")

    def test_every_question_is_answered_in_the_shape_parse_reply_reads(self) -> None:
        status, headers, answer = self.worker.http("POST", "/decide", ALERT)
        self.assertEqual((status, headers["Content-Type"]), (200, "application/json"))
        self.assertEqual((answer["model_id"], answer["model_revision"]), ("fake-decider", REVISION))
        self.assertEqual([a["name"] for a in answer["answers"]], ["severity", "needs_human"])
        for a, options in zip(answer["answers"], (["info", "warning", "critical"], ["false", "true"])):
            with self.subTest(a["name"]):
                self.assertEqual(sorted(a), ["act_probability", "confidence", "name", "probabilities"])
                self.assertEqual(list(a["probabilities"]), options)
                self.assertAlmostEqual(sum(a["probabilities"].values()), 1.0, delta=1e-6)
                p = np.array(list(a["probabilities"].values()))
                self.assertAlmostEqual(a["confidence"], confidence_from_probs(p, len(p)), places=12)
                self.assertLess(a["confidence"], max(a["probabilities"].values()), "not the top probability")

    def test_the_dashboards_template_with_a_score_range_is_answered(self) -> None:
        """The e2e that found the defect: the dashboard's template, as the orchestrator sends it."""
        template = {"text": "oom restart restart", "questions": [
            {"name": "severity", "type": "choice", "options": ["low", "medium", "high", "critical"]},
            {"name": "fraud", "type": "noul", "options": ["false", "true"]},
            {"name": "risk_score", "type": "score", "options": ["1", "2", "3", "4", "5"],
             "range": {"min": 1, "max": 5}}]}
        status, _, answer = self.worker.http("POST", "/decide", template)
        self.assertEqual(status, 200, answer)
        risk = answer["answers"][2]
        self.assertEqual((risk["name"], list(risk["probabilities"])), ("risk_score", ["1", "2", "3", "4", "5"]))
        self.assertAlmostEqual(sum(risk["probabilities"].values()), 1.0, delta=1e-9)
        contradicted = {**template, "questions": [{**template["questions"][2], "range": {"min": 1, "max": 3}}]}
        status, _, body = self.worker.http("POST", "/decide", contradicted)
        self.assertEqual((status, body["error_type"]), (400, "Validation"))
        self.assertIn("contradicts its options", body["error"])

    def test_the_answer_is_the_models_not_a_constant(self) -> None:
        other = {**ALERT, "text": "disk ok"}
        first = self.worker.http("POST", "/decide", ALERT)[2]["answers"][0]["probabilities"]
        second = self.worker.http("POST", "/decide", other)[2]["answers"][0]["probabilities"]
        self.assertNotEqual(first, second)
        self.assertEqual(first, self.worker.http("POST", "/decide", ALERT)[2]["answers"][0]["probabilities"])

    def test_each_refusal_carries_its_status_and_error_type(self) -> None:
        yes_no = {**ALERT, "questions": [{"name": "fraud", "type": "noul", "options": ["yes", "no"]}]}
        cases = [
            ("a body that is not JSON", b"{text", 400, "Validation"),
            ("no questions", {**ALERT, "questions": []}, 400, "Validation"),
            ("a noul with labels the model does not answer in", yes_no, 400, "Validation"),
        ]
        for name, body, status, error_type in cases:
            with self.subTest(name):
                got, _, answer = self.worker.http("POST", "/decide", body)
                self.assertEqual((got, answer["error_type"]), (status, error_type))
                self.assertTrue(answer["error"])

    def test_the_wrong_method_is_refused_on_every_route(self) -> None:
        for method, path in (("GET", "/decide"), ("POST", "/info"), ("POST", "/health")):
            with self.subTest(f"{method} {path}"):
                status, headers, _ = self.worker.http(method, path, {} if method == "POST" else None)
                self.assertEqual(status, 405)
                self.assertIn("Allow", headers)

    def test_info_names_the_model_its_revision_and_engine(self) -> None:
        status, _, info = self.worker.http("GET", "/info")
        self.assertEqual(status, 200)
        self.assertEqual((info["model_id"], info["model_revision"], info["engine"]),
                         ("fake-decider", REVISION, "onnx_decision"))

    def test_metrics_share_the_port(self) -> None:
        self.worker.http("POST", "/decide", ALERT)
        status, _, body = self.worker.http("GET", "/metrics")
        self.assertEqual(status, 200)
        self.assertIn(b"fake-decider", body)

    def test_an_engine_failure_is_a_backend_error_and_an_unfit_question_the_engines_refusal(self) -> None:
        cases = [
            ("the engine raises", RuntimeError("the graph broke"), 424, "Backend", "RuntimeError"),
            ("options that do not fit the head", InvalidRequest("do not fit"), 422, "Validation", "do not fit"),
        ]
        for name, failure, status, error_type, message in cases:
            with self.subTest(name):
                FakeDecisionEngine.reset()
                FakeDecisionEngine.fail_with = failure
                got, _, answer = self.worker.http("POST", "/decide", ALERT)
                self.assertEqual((got, answer["error_type"]), (status, error_type))
                self.assertIn(message, answer["error"])
        FakeDecisionEngine.reset()
        self.assertEqual(self.worker.http("POST", "/decide", ALERT)[0], 200, "a failure kept the slot")


class BoundedWorkTest(unittest.TestCase):
    """A request the model cannot finish in time is refused or stopped, never left running."""

    def test_planned_work_over_the_budget_is_a_413_before_the_model_runs(self) -> None:
        worker = Harness(self, max_planned_tokens=10)
        worker.dip("load", id="fake-decider")
        status, _, body = worker.http("POST", "/decide", ALERT)
        self.assertEqual((status, body["error_type"]), (413, "Validation"))
        self.assertIn("over the 10", body["error"])
        self.assertEqual(worker.http("POST", "/decide", ALERT)[0], 413, "a refusal kept the only slot")

    def test_a_request_past_its_deadline_is_a_504_with_nothing_returned(self) -> None:
        worker = Harness(self, deadline_s=-1.0)
        worker.dip("load", id="fake-decider")
        status, _, body = worker.http("POST", "/decide", ALERT)
        self.assertEqual((status, body["error_type"]), (504, "Timeout"))
        self.assertIn("after 0 of 1 batches", body["error"])
        self.assertNotIn("answers", body)

    def test_info_states_the_bounds(self) -> None:
        worker = Harness(self)
        worker.dip("load", id="fake-decider")
        info = worker.http("GET", "/info")[2]
        self.assertEqual((info["max_concurrent_requests"], info["max_planned_tokens"], info["deadline_s"]),
                         (1, decide.MAX_PLANNED_TOKENS, decide.DEADLINE_S))
        self.assertLess(decide.DEADLINE_S, 120, "must stop before the orchestrator's 120 s timeout")


class OverloadTest(unittest.TestCase):
    # Not a table row: a request held inside the engine while a second one arrives.
    def test_past_the_concurrency_cap_the_answer_is_429_not_a_queue(self) -> None:
        worker = Harness(self)
        self.assertEqual(decide.MAX_CONCURRENT_REQUESTS, 1, "one lock: a second slot would only queue")
        worker.dip("load", id="fake-decider")
        FakeDecisionEngine.gate = threading.Event()
        FakeDecisionEngine.entered = threading.Event()
        first = []
        holder = threading.Thread(target=lambda: first.append(worker.http("POST", "/decide", ALERT)))
        holder.start()
        self.assertTrue(FakeDecisionEngine.entered.wait(timeout=5))

        status, headers, answer = worker.http("POST", "/decide", ALERT)
        self.assertEqual((status, answer["error_type"], headers["Retry-After"]), (429, "Overloaded", "1"))

        FakeDecisionEngine.gate.set()
        holder.join(timeout=10)
        self.assertEqual(first[0][0], 200)
        self.assertEqual(worker.http("POST", "/decide", ALERT)[0], 200, "the slot was not returned")


class ServiceIdentityTest(unittest.TestCase):
    """The wiring: what this service hands the framework, and a manifest the engine can serve."""

    def setUp(self) -> None:
        self.registry = load_registry(MANIFEST)
        self.spec = self.registry.get(self.registry.default_model)

    def test_what_the_worker_advertises(self) -> None:
        self.assertEqual((WORKER.name, WORKER.prog, WORKER.engines),
                         ("inferences-system-one", "system_one_worker", ENGINE_NAMES))
        self.assertEqual(sorted(WORKER.routes), ["/decide", "/health", "/info"])
        self.assertEqual(WORKER.registry_path, MANIFEST)
        self.assertEqual(WORKER.default_socket_path, "/run/dita/inferences-system-one.sock")

    def test_the_one_model_is_laya_multilingual_at_the_spikes_revision(self) -> None:
        self.assertEqual(list(self.registry.models), ["laya-multilingual"])
        self.assertEqual(self.spec.engine, "onnx_decision")
        self.assertEqual({(f.repo, f.revision) for f in self.spec.files},
                         {("convaiinnovations/laya-multilingual", "b4a904d1a2a54c822b829e24291d4b8f280fe43e")})

    def test_every_file_the_engine_opens_is_pinned_and_the_encoder_is_tied_to_the_weights(self) -> None:
        files = {f.dest: f for f in self.spec.files}
        options = self.spec.options
        for key in ("weights", "config", "tokenizer", "tokenizer_config"):
            with self.subTest(key):
                self.assertIn(options[key], files)
        self.assertEqual(options["encoder_built_from"], files[options["weights"]].sha256)
        self.assertEqual(files[options["weights"]].sha256,
                         "9d628fd971b700382ac6f65920a86f149777b2e748e0c955fb3b19695aa8f204")

    def test_every_file_is_pinned(self) -> None:
        for pinned in self.spec.files:
            with self.subTest(pinned.dest):
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
