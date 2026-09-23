"""Service routes on the metrics port: the framework reads and bounds the body, the route
owns the answer, and a service that supplies none sees exactly the old surface."""

from __future__ import annotations

import http.client
import shutil
import tempfile
import unittest
from pathlib import Path

from worker import (
    MAX_REQUEST_BODY,
    Metrics,
    ModelManager,
    NoModelLoaded,
    Response,
    json_response,
    metrics as metrics_mod,
)

from .support import build_fake_engine, fake_worker, registry_in, write_registry


def echo_route(manager, method, body):
    """What arrived, reflected, plus which model answered: enough to see every hop."""
    try:
        model_id, value, _ = manager.run(lambda engine: engine.name)
    except NoModelLoaded:
        return json_response(503, {"error": "nothing resident"})
    return json_response(200, {"method": method, "body": body.decode(), "model": model_id,
                               "engine": value}, headers=(("X-Route", "echo"),))


def broken_route(manager, method, body):
    raise RuntimeError("the route itself is wrong")


class RoutesTest(unittest.TestCase):
    def setUp(self) -> None:
        models_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, models_dir, True)
        self.manager = ModelManager(registry_in(models_dir), models_dir, build_fake_engine, Metrics())
        routes = {"/echo": echo_route, "/broken": broken_route}
        server = metrics_mod.serve(Metrics(), "inferences-test", self.manager, ("127.0.0.1", 0),
                                   routes=routes)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.address = server.server_address

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection(*self.address, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def test_a_route_answers_both_methods_through_the_resident_engine(self) -> None:
        self.manager.load("beta")
        cases = [
            ("post carries its body", "POST", b"hello", b'"body": "hello"'),
            ("get carries none", "GET", None, b'"body": ""'),
        ]
        for name, method, body, expected in cases:
            with self.subTest(name):
                status, headers, payload = self.request(method, "/echo", body)
                self.assertEqual(status, 200)
                self.assertIn(expected, payload)
                self.assertIn(f'"method": "{method}"'.encode(), payload)
                self.assertIn(b'"model": "beta", "engine": "reverse"', payload)
                self.assertEqual(headers["X-Route"], "echo")
                self.assertEqual(headers["Content-Type"], "application/json")

    def test_the_route_decides_what_nothing_resident_means(self) -> None:
        status, _, payload = self.request("POST", "/echo", b"x")
        self.assertEqual((status, payload), (503, b'{"error": "nothing resident"}'))

    def test_what_the_framework_refuses_before_a_route_runs(self) -> None:
        cases = [
            ("no route at that path", "POST", "/nowhere", b"x", 404),
            ("metrics take no POST", "POST", "/metrics", b"x", 404),
            ("a raising route", "GET", "/broken", None, 500),
        ]
        for name, method, path, body, expected in cases:
            with self.subTest(name):
                status, _, _ = self.request(method, path, body)
                self.assertEqual(status, expected)

    def test_a_body_is_refused_on_its_declared_length_alone(self) -> None:
        """Headers only, no body: the refusal must come before a single body byte is read,
        which is also why a client that sends the whole oversized body may see a reset."""
        cases = [
            ("no length", None, 411),
            ("not a number", "ten", 411),
            ("one byte over the cap", str(MAX_REQUEST_BODY + 1), 413),
        ]
        for name, length, expected in cases:
            with self.subTest(name):
                conn = http.client.HTTPConnection(*self.address, timeout=5)
                try:
                    conn.putrequest("POST", "/echo")
                    if length is not None:
                        conn.putheader("Content-Length", length)
                    conn.endheaders()
                    self.assertEqual(conn.getresponse().status, expected)
                finally:
                    conn.close()

    def test_a_body_exactly_at_the_cap_reaches_the_route(self) -> None:
        self.manager.load("alpha")
        status, _, payload = self.request("POST", "/echo", b"y" * MAX_REQUEST_BODY)
        self.assertEqual(status, 200)
        self.assertIn(b"y" * 64, payload)

    def test_scrapes_still_answer_beside_the_routes(self) -> None:
        status, headers, payload = self.request("GET", "/metrics")
        self.assertEqual(status, 200)
        self.assertIn("version=0.0.4", headers["Content-Type"])
        self.assertIn(b"dita_worker_uptime_seconds", payload)


class WorkerRoutesTest(unittest.TestCase):
    def test_a_worker_that_names_no_routes_serves_none(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        worker = fake_worker(write_registry(Path(directory.name)))
        self.assertEqual(dict(worker.routes), {})
        with self.assertRaises(TypeError):
            worker.routes["/x"] = echo_route  # type: ignore[index]

    def test_a_response_defaults_to_json(self) -> None:
        self.assertEqual(Response(200, b"{}").content_type, "application/json")
        self.assertEqual(json_response(422, {"a": 1}).body, b'{"a": 1}')


if __name__ == "__main__":
    unittest.main()
