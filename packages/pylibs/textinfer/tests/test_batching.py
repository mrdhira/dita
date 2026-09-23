"""Batch planning, padding and graph feeds: one row per case."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import onnxruntime

from textinfer import feedable, feeds, open_session, pad, plan_batches


class PlanBatchesTest(unittest.TestCase):
    def test_batches(self) -> None:
        cases = [
            ("all fit in one", [2, 3, 1], 12, [[1, 0, 2]]),
            ("the budget splits them", [5, 5, 5], 10, [[0, 1], [2]]),
            ("longest first, so padding stays small", [1, 6, 1, 6], 12, [[1, 3], [0, 2]]),
            ("an input over the budget still gets a batch", [20, 1], 12, [[0], [1]]),
        ]
        for name, lengths, budget, expected in cases:
            with self.subTest(name):
                batches = plan_batches(lengths, budget)
                self.assertEqual(batches, expected)
                self.assertEqual(sorted(i for batch in batches for i in batch), list(range(len(lengths))))
                for batch in (batch for batch in batches if len(batch) > 1):
                    self.assertLessEqual(len(batch) * max(lengths[i] for i in batch), budget)

    def test_pad(self) -> None:
        ids, mask = pad([[7, 8, 9], [5]], pad_id=0)
        np.testing.assert_array_equal(ids, [[7, 8, 9], [5, 0, 0]])
        np.testing.assert_array_equal(mask, [[1, 1, 1], [1, 0, 0]])
        self.assertEqual((ids.dtype, mask.dtype), (np.int64, np.int64))


class FeedsTest(unittest.TestCase):
    def test_every_input_a_text_graph_can_ask_for(self) -> None:
        ids, mask = pad([[7, 8, 9, 4], [5, 6]], pad_id=0)
        inputs = [("input_ids", ["b", "l"]), ("attention_mask", ["b", "t"]),
                  ("token_type_ids", ["b", "l"]), ("position_ids", ["b", "l"]),
                  ("past_key_values.0.key", ["b", 8, "p", 128])]
        fed = feeds(inputs, ids, mask)
        self.assertEqual(sorted(fed), sorted(name for name, _ in inputs))
        self.assertIs(fed["input_ids"], ids)
        self.assertIs(fed["attention_mask"], mask)
        np.testing.assert_array_equal(fed["token_type_ids"], np.zeros((2, 4)))
        np.testing.assert_array_equal(fed["position_ids"], [[0, 1, 2, 3], [0, 1, 1, 1]])
        self.assertEqual(fed["past_key_values.0.key"].shape, (2, 8, 0, 128))
        self.assertEqual(fed["past_key_values.0.key"].dtype, np.float32)

    def test_only_what_it_knows_how_to_fill_is_feedable(self) -> None:
        cases = [("input_ids", True), ("past_key_values.3.value", True), ("pixel_values", False),
                 ("past_key_value", False)]
        for name, expected in cases:
            with self.subTest(name):
                self.assertEqual(feedable(name), expected)
        with self.assertRaises(ValueError):
            feeds([("pixel_values", [])], *pad([[1]], 0))


class SessionTest(unittest.TestCase):
    def test_the_session_is_cpu_arena_free_and_threaded_as_asked(self) -> None:
        with mock.patch.object(onnxruntime, "InferenceSession") as session:
            open_session(Path("/models/x/model.onnx"), threads=3)
        (path,), kwargs = session.call_args
        self.assertEqual(path, "/models/x/model.onnx")
        self.assertFalse(kwargs["sess_options"].enable_cpu_mem_arena)
        self.assertEqual(kwargs["sess_options"].intra_op_num_threads, 3)
        self.assertEqual(kwargs["providers"], ["CPUExecutionProvider"])


if __name__ == "__main__":
    unittest.main()
