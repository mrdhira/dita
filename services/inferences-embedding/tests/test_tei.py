"""TEI request parsing, one row per shape a client can send."""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from embedding_worker import tei
from embedding_worker.engines.onnx_embedder import EmbedRequest


class ParseEmbedTest(unittest.TestCase):
    def test_what_is_accepted(self) -> None:
        cases = [
            ("a list with defaults", {"inputs": ["a", "b"]}, EmbedRequest(texts=["a", "b"])),
            ("one string", {"inputs": "a"}, EmbedRequest(texts=["a"])),
            ("an empty string is still an input", {"inputs": [""]}, EmbedRequest(texts=[""])),
            ("TEI's capitalised direction", {"inputs": ["a"], "truncation_direction": "Left"},
             EmbedRequest(texts=["a"], truncation_direction="left")),
            ("nulls mean the defaults",
             {"inputs": ["a"], "truncate": None, "normalize": None, "prompt_name": None, "dimensions": None},
             EmbedRequest(texts=["a"])),
            ("every option set",
             {"inputs": ["a"], "truncate": True, "normalize": False, "prompt_name": "query", "dimensions": 256},
             EmbedRequest(texts=["a"], truncate=True, normalize=False, prompt_name="query", dimensions=256)),
            ("exactly the batch limit", {"inputs": ["a"] * tei.MAX_CLIENT_BATCH_SIZE},
             EmbedRequest(texts=["a"] * tei.MAX_CLIENT_BATCH_SIZE)),
        ]
        for name, body, expected in cases:
            with self.subTest(name):
                self.assertEqual(tei.parse_embed(json.dumps(body).encode()), expected)

    def test_what_is_refused(self) -> None:
        cases = [
            ("not JSON", b"{", "Validation", "not JSON"),
            ("not UTF-8", b"\xff\xfe", "Validation", "not JSON"),
            ("not an object", b"[1]", "Validation", "JSON object"),
            ("no inputs", b"{}", "Validation", "missing field `inputs`"),
            ("an unknown field", b'{"inputs": ["a"], "input_type": "query"}', "Validation", "input_type"),
            ("token ids", b'{"inputs": [[101, 102]]}', "Validation", "token id"),
            ("a number", b'{"inputs": 3}', "Validation", "string or a list"),
            ("an empty list", b'{"inputs": []}', "Empty", "cannot be empty"),
            ("one past the batch limit", json.dumps({"inputs": ["a"] * 33}).encode(), "Validation", "batch size 33"),
            ("a direction nobody has", b'{"inputs": ["a"], "truncation_direction": "middle"}', "Validation", "left"),
            ("zero dimensions", b'{"inputs": ["a"], "dimensions": 0}', "Validation", "positive"),
            ("boolean dimensions", b'{"inputs": ["a"], "dimensions": true}', "Validation", "positive"),
            ("a numeric prompt name", b'{"inputs": ["a"], "prompt_name": 1}', "Validation", "prompt_name"),
            ("a string for a flag", b'{"inputs": ["a"], "normalize": "yes"}', "Validation", "`normalize`"),
            ("a number for a flag", b'{"inputs": ["a"], "truncate": 1}', "Validation", "`truncate`"),
        ]
        for name, body, error_type, message in cases:
            with self.subTest(name):
                with self.assertRaises(tei.TeiError) as caught:
                    tei.parse_embed(body)
                self.assertEqual(caught.exception.error_type, error_type)
                self.assertIn(message, str(caught.exception))

    def test_each_error_type_has_teis_status(self) -> None:
        cases = [("Unhealthy", 503), ("Backend", 424), ("Overloaded", 429), ("Validation", 422),
                 ("Tokenizer", 422), ("Empty", 400)]
        for error_type, status in cases:
            with self.subTest(error_type):
                response = tei.TeiError(error_type, "why").response()
                self.assertEqual(response.status, status)
                self.assertEqual(json.loads(response.body), {"error": "why", "error_type": error_type})
                self.assertEqual(dict(response.headers).get("Retry-After"),
                                 "1" if error_type == "Overloaded" else None)


class NothingResidentTest(unittest.TestCase):
    def test_info_and_health_say_unhealthy_rather_than_guess_a_model(self) -> None:
        routes = tei.routes()
        manager = SimpleNamespace(resident=lambda: None)
        for path in ("/info", "/health"):
            with self.subTest(path):
                response = routes[path](manager, "GET", b"")
                self.assertEqual(response.status, 503)
                self.assertEqual(json.loads(response.body)["error_type"], "Unhealthy")


if __name__ == "__main__":
    unittest.main()
