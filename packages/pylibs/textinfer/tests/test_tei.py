"""TEI's error contract and the admission rule every route follows."""

from __future__ import annotations

import json
import threading
import time
import unittest

from textinfer import (
    Admission,
    InputTooLong,
    InvalidRequest,
    TeiError,
    flag,
    method_not_allowed,
    truncation_direction,
    unhealthy,
)
from worker import NoModelLoaded


class FakeManager:
    """`ModelManager.run` as a route sees it: the work, or the failure it raises."""

    def __init__(self, fail: BaseException | None = None, gate: threading.Event | None = None) -> None:
        self.fail, self.gate = fail, gate
        self.entered = threading.Event()

    def run(self, work):
        self.entered.set()
        if self.gate is not None:
            self.gate.wait(timeout=10)
        if self.fail is not None:
            raise self.fail
        return "model-x", work("engine"), 1.5


class TeiErrorTest(unittest.TestCase):
    def test_each_error_type_has_teis_status(self) -> None:
        cases = [("Unhealthy", 503), ("Backend", 424), ("Overloaded", 429), ("Validation", 422),
                 ("Tokenizer", 422), ("Empty", 400)]
        for error_type, status in cases:
            with self.subTest(error_type):
                response = TeiError(error_type, "why").response()
                self.assertEqual(response.status, status)
                self.assertEqual(json.loads(response.body), {"error": "why", "error_type": error_type})
                self.assertEqual(dict(response.headers).get("Retry-After"),
                                 "1" if error_type == "Overloaded" else None)

    def test_the_fixed_refusals(self) -> None:
        self.assertEqual((unhealthy().status, json.loads(unhealthy().body)["error_type"]), (503, "Unhealthy"))
        refused = method_not_allowed("POST")
        self.assertEqual((refused.status, dict(refused.headers)["Allow"]), (405, "POST"))


class FieldTest(unittest.TestCase):
    def test_flag(self) -> None:
        cases = [("absent", {}, True), ("null", {"x": None}, True), ("false", {"x": False}, False)]
        for name, raw, expected in cases:
            with self.subTest(name):
                self.assertEqual(flag(raw, "x", True), expected)
        for bad in ("yes", 1, 0):
            with self.subTest(repr(bad)), self.assertRaises(TeiError):
                flag({"x": bad}, "x", True)

    def test_truncation_direction(self) -> None:
        cases = [({}, "right"), ({"truncation_direction": "Left"}, "left"),
                 ({"truncation_direction": "right"}, "right")]
        for raw, expected in cases:
            with self.subTest(raw):
                self.assertEqual(truncation_direction(raw), expected)
        for bad in ("middle", 1, None):
            with self.subTest(repr(bad)), self.assertRaises(TeiError):
                truncation_direction({"truncation_direction": bad})


class AdmissionTest(unittest.TestCase):
    def test_every_failure_becomes_teis_type_and_the_slot_comes_back(self) -> None:
        admission = Admission(1)
        cases = [
            ("nothing resident", NoModelLoaded("load first"), "Unhealthy"),
            ("the caller's mistake", InvalidRequest("bad prompt"), "Validation"),
            ("an input too long", InputTooLong(0, 9, 8), "Validation"),
            ("the engine broke", RuntimeError("graph"), "Backend"),
            ("a value error inside the graph", ValueError("shape"), "Backend"),
        ]
        for name, failure, error_type in cases:
            with self.subTest(name):
                with self.assertRaises(TeiError) as caught:
                    admission.run(FakeManager(fail=failure), lambda engine: engine)
                self.assertEqual(caught.exception.error_type, error_type)
                self.assertEqual(admission.run(FakeManager(), lambda engine: engine + "!"),
                                 ("model-x", "engine!", 1.5), "the slot was not returned")

    # Not a table row: one request held inside the model while another arrives.
    def test_past_the_limit_the_answer_is_overloaded_not_a_wait(self) -> None:
        admission = Admission(1)
        held = FakeManager(gate=threading.Event())
        thread = threading.Thread(target=admission.run, args=(held, lambda engine: engine))
        thread.start()
        self.assertTrue(held.entered.wait(timeout=5))

        started = time.monotonic()
        with self.assertRaises(TeiError) as caught:
            admission.run(FakeManager(), lambda engine: engine)
        self.assertEqual(caught.exception.error_type, "Overloaded")
        self.assertLess(time.monotonic() - started, 0.2, "it waited for the slot instead of refusing")

        held.gate.set()
        thread.join(timeout=10)
        self.assertEqual(admission.run(FakeManager(), lambda engine: engine)[1], "engine")


if __name__ == "__main__":
    unittest.main()
