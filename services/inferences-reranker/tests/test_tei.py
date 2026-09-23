"""TEI rerank parsing and the response, one row per shape a client can send."""

from __future__ import annotations

import json
import unittest

import numpy as np
from textinfer import TeiError

from reranker_worker import tei
from reranker_worker.engines.cross_encoder import RerankRequest


class ParseRerankTest(unittest.TestCase):
    def test_what_is_accepted(self) -> None:
        cases = [
            ("Hindsight's request", {"query": "q", "texts": ["a", "b"], "return_text": False}, False,
             RerankRequest(query="q", texts=["a", "b"])),
            ("truncate absent takes the server's auto_truncate", {"query": "q", "texts": ["a"]}, True,
             RerankRequest(query="q", texts=["a"], truncate=True)),
            ("truncate null does too", {"query": "q", "texts": ["a"], "truncate": None}, True,
             RerankRequest(query="q", texts=["a"], truncate=True)),
            ("an explicit false beats auto_truncate", {"query": "q", "texts": ["a"], "truncate": False}, True,
             RerankRequest(query="q", texts=["a"], truncate=False)),
            ("TEI's capitalised direction", {"query": "q", "texts": ["a"], "truncation_direction": "Left"},
             False, RerankRequest(query="q", texts=["a"], truncation_direction="left")),
            ("every option", {"query": "", "texts": [""], "truncate": True, "raw_scores": True,
                              "return_text": True}, False, RerankRequest(query="", texts=[""], truncate=True)),
            ("exactly the batch limit", {"query": "q", "texts": ["a"] * tei.MAX_CLIENT_BATCH_SIZE}, False,
             RerankRequest(query="q", texts=["a"] * tei.MAX_CLIENT_BATCH_SIZE)),
        ]
        for name, raw, auto_truncate, expected in cases:
            with self.subTest(name):
                self.assertEqual(tei.parse_rerank(raw, auto_truncate), expected)

    def test_what_is_refused(self) -> None:
        cases = [
            ("an unknown field", {"query": "q", "texts": ["a"], "top_n": 3}, "Validation", "top_n"),
            ("no query", {"texts": ["a"]}, "Validation", "missing field `query`"),
            ("no texts", {"query": "q"}, "Validation", "missing field `texts`"),
            ("a numeric query", {"query": 1, "texts": ["a"]}, "Validation", "`query` must be a string"),
            ("one string for texts", {"query": "q", "texts": "a"}, "Validation", "list of strings"),
            ("token ids", {"query": "q", "texts": [[1, 2]]}, "Validation", "list of strings"),
            ("an empty list", {"query": "q", "texts": []}, "Empty", "`texts` cannot be empty"),
            ("one past the batch limit", {"query": "q", "texts": ["a"] * 33}, "Validation", "batch size 33"),
            ("a string for raw_scores", {"query": "q", "texts": ["a"], "raw_scores": "yes"}, "Validation", "raw_scores"),
            ("a number for return_text", {"query": "q", "texts": ["a"], "return_text": 1}, "Validation", "return_text"),
            ("a direction nobody has", {"query": "q", "texts": ["a"], "truncation_direction": "up"}, "Validation", "left"),
        ]
        for name, raw, error_type, message in cases:
            with self.subTest(name):
                with self.assertRaises(TeiError) as caught:
                    tei.parse_rerank(raw, False)
                self.assertEqual(caught.exception.error_type, error_type)
                self.assertIn(message, str(caught.exception))


class RankTest(unittest.TestCase):
    LOGITS = np.array([-1.0, 2.0, 0.0, 2.0], dtype=np.float32)
    TEXTS = ["w", "x", "y", "z"]

    def test_best_first_with_ties_in_request_order(self) -> None:
        self.assertEqual([e["index"] for e in tei.rank(self.LOGITS, self.TEXTS, False, False)], [1, 3, 2, 0])

    def test_scores_are_the_sigmoid_unless_raw_scores(self) -> None:
        cases = [
            ("sigmoid", False, [0.8807971, 0.8807971, 0.5, 0.26894143]),
            ("raw logits", True, [2.0, 2.0, 0.0, -1.0]),
        ]
        for name, raw_scores, expected in cases:
            with self.subTest(name):
                ranks = tei.rank(self.LOGITS, self.TEXTS, raw_scores, False)
                self.assertEqual([e["score"] for e in ranks], expected)

    def test_text_only_when_asked(self) -> None:
        self.assertNotIn("text", tei.rank(self.LOGITS, self.TEXTS, False, False)[0])
        self.assertEqual([e["text"] for e in tei.rank(self.LOGITS, self.TEXTS, False, True)], ["x", "z", "y", "w"])

    def test_a_score_is_float32_at_its_shortest_spelling(self) -> None:
        (entry,) = tei.rank(np.array([1.0], dtype=np.float32), ["a"], False, False)
        self.assertEqual(json.dumps(entry["score"]), "0.7310586")

    def test_a_nan_is_a_backend_error(self) -> None:
        with self.assertRaises(TeiError) as caught:
            tei.rank(np.array([np.nan, 1.0], dtype=np.float32), ["a", "b"], False, False)
        self.assertEqual((caught.exception.error_type, str(caught.exception)), ("Backend", "score is NaN"))


if __name__ == "__main__":
    unittest.main()
