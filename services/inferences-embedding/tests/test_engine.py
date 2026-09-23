"""The embedding logic: pooling, Matryoshka, batching, truncation and what reaches the graph.

Tables for the pure functions, one row per case. The Embedder scenarios drive a real
tokenizer and a fake session, because what matters is which ids and inputs the graph saw.
"""

from __future__ import annotations

import unittest
from pathlib import Path

import numpy as np

from embedding_worker.engines import ENGINE_NAMES, build_engine
from embedding_worker.engines.onnx_embedder import (
    EmbedderConfig,
    EmbedRequest,
    InputTooLong,
    InvalidRequest,
    OnnxEmbedder,
    finish,
    pad,
    plan_batches,
    pool,
)
from worker import UnknownEngine

from .support import OPTIONS, VOCAB, WIDTH, FakeSession, Input, embedder

HIDDEN = np.array([[[1.0, 0.0], [3.0, 2.0], [100.0, 100.0]],
                   [[5.0, 6.0], [100.0, 100.0], [100.0, 100.0]]], dtype=np.float32)


class PoolTest(unittest.TestCase):
    def test_pooling(self) -> None:
        right = np.array([[1, 1, 0], [1, 0, 0]])
        left = np.array([[0, 1, 1], [0, 0, 1]])
        cases = [
            ("mean ignores padded positions", "mean", HIDDEN, right, [[2.0, 1.0], [5.0, 6.0]]),
            ("last token with right padding", "last_token", HIDDEN, right, [[3.0, 2.0], [5.0, 6.0]]),
            ("last token with left padding", "last_token", HIDDEN, left, [[100.0, 100.0], [100.0, 100.0]]),
            ("graph output passes through", "graph", HIDDEN[:, 0], right, [[1.0, 0.0], [5.0, 6.0]]),
        ]
        for name, strategy, output, mask, expected in cases:
            with self.subTest(name):
                np.testing.assert_allclose(pool(strategy, output, mask), expected)

    def test_an_unknown_strategy_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            pool("cls", HIDDEN, np.ones((2, 3)))


class FinishTest(unittest.TestCase):
    def test_matryoshka_and_normalisation(self) -> None:
        pooled = np.array([[3.0, 4.0, 12.0, 0.0]], dtype=np.float32)
        centred = pooled - pooled.mean()
        layer_normed = centred / np.sqrt((centred**2).mean() + 1e-5)
        cases = [
            ("full width, normalised", 4, True, False, pooled / 13.0),
            ("full width, raw", 4, False, False, pooled),
            ("truncated, then normalised", 2, True, False, [[0.6, 0.8]]),
            ("truncated, raw", 2, False, False, [[3.0, 4.0]]),
            ("layer norm comes before the cut",
             2, True, True, layer_normed[:, :2] / np.linalg.norm(layer_normed[:, :2])),
        ]
        for name, dimensions, normalize, layer_norm, expected in cases:
            with self.subTest(name):
                result = finish(pooled.copy(), dimensions, normalize, layer_norm)
                np.testing.assert_allclose(result, expected, rtol=1e-6, atol=1e-6)
                self.assertEqual(result.dtype, np.float32)

    def test_a_zero_vector_stays_finite(self) -> None:
        result = finish(np.zeros((1, 4), dtype=np.float32), 4, True, False)
        self.assertTrue(np.isfinite(result).all())


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


class ConfigTest(unittest.TestCase):
    def test_what_a_config_refuses(self) -> None:
        cases = [
            ("a missing field", {"output": None}, "missing output"),
            ("an unknown pooling", {"pooling": "cls"}, "pooling 'cls'"),
            ("dimensions as a string", {"dimensions": "768"}, "dimensions must be a positive"),
            ("a boolean limit", {"max_input_tokens": True}, "max_input_tokens must be a positive"),
            ("an input that cannot fit a batch", {"max_input_tokens": 13}, "cannot exceed max_batch_tokens"),
            ("a limit past the model card", {"max_sequence_length": 4}, "max_sequence_length"),
            ("a prompt that is not text", {"prompts": {"query": 3}}, "prompts must map"),
        ]
        for name, change, message in cases:
            with self.subTest(name):
                options = {**OPTIONS, **change}
                options = {key: value for key, value in options.items() if value is not None}
                with self.assertRaises(ValueError) as caught:
                    EmbedderConfig.from_options(options)
                self.assertIn(message, str(caught.exception))

    def test_a_valid_config_keeps_what_it_was_given(self) -> None:
        config = EmbedderConfig.from_options({**OPTIONS, "matryoshka_dimensions": [8, 4]})
        self.assertEqual((config.dimensions, config.matryoshka_dimensions), (WIDTH, (8, 4)))
        self.assertFalse(config.matryoshka_layer_norm)


class EmbedderTest(unittest.TestCase):
    def test_vectors_come_back_in_input_order_across_batches(self) -> None:
        session = FakeSession()
        texts = ["alpha", "beta beta beta beta", "gamma", "delta delta"]
        vectors = embedder(session).embed(EmbedRequest(texts=texts, normalize=False))

        self.assertGreater(len(session.calls), 1, "the budget should have split this request")
        self.assertEqual(vectors.shape, (4, WIDTH))
        for row, word in enumerate(["alpha", "beta", "gamma", "delta"]):
            with self.subTest(word):
                self.assertGreater(vectors[row, VOCAB[word]], 0)
                others = [VOCAB[w] for w in ("alpha", "beta", "gamma", "delta") if w != word]
                self.assertTrue(all(vectors[row, column] == 0 for column in others))

    def test_padding_never_reaches_a_mean(self) -> None:
        alone = embedder().embed(EmbedRequest(texts=["alpha"], normalize=False))
        beside_a_longer_one = embedder().embed(
            EmbedRequest(texts=["alpha", "beta beta beta"], normalize=False))
        np.testing.assert_allclose(beside_a_longer_one[0], alone[0])
        self.assertEqual(beside_a_longer_one[0, VOCAB["[PAD]"]], 0)

    def test_the_prompt_is_prepended_and_tokenised_with_the_text(self) -> None:
        session = FakeSession()
        embedder(session).embed(EmbedRequest(texts=["alpha"], prompt_name="query"))
        self.assertEqual(session.calls[0]["input_ids"].tolist(),
                         [[VOCAB["[CLS]"], VOCAB["search_query:"], VOCAB["alpha"], VOCAB["[SEP]"]]])

    def test_no_prompt_name_means_no_prompt(self) -> None:
        session = FakeSession()
        embedder(session).embed(EmbedRequest(texts=["alpha"]))
        self.assertEqual(session.calls[0]["input_ids"].tolist(), [[2, VOCAB["alpha"], 3]])

    def test_what_a_request_is_refused_for(self) -> None:
        cases = [
            ("an unknown prompt", EmbedRequest(texts=["alpha"], prompt_name="passage"), "known: document, query"),
            ("dimensions past the model", EmbedRequest(texts=["alpha"], dimensions=WIDTH + 1), "between 1 and 16"),
            ("an input past the limit", EmbedRequest(texts=["alpha", "beta " * 5]), "Given: 7 (input 1)"),
        ]
        for name, request, message in cases:
            with self.subTest(name):
                with self.assertRaises(InvalidRequest) as caught:
                    embedder().embed(request)
                self.assertIn(message, str(caught.exception))

    def test_an_input_exactly_at_the_limit_is_embedded_whole(self) -> None:
        session = FakeSession()
        embedder(session).embed(EmbedRequest(texts=["beta " * 4]))
        self.assertEqual(session.calls[0]["input_ids"].shape, (1, OPTIONS["max_input_tokens"]))

    def test_the_layer_norm_is_only_for_a_shorter_vector(self) -> None:
        plain = embedder().embed(EmbedRequest(texts=["alpha beta"]))
        full = embedder(matryoshka_layer_norm=True).embed(EmbedRequest(texts=["alpha beta"]))
        cut = embedder(matryoshka_layer_norm=True).embed(EmbedRequest(texts=["alpha beta"], dimensions=8))
        np.testing.assert_allclose(full, plain)
        self.assertFalse(np.allclose(cut, plain[:, :8] / np.linalg.norm(plain[:, :8])))

    def test_an_input_past_the_limit_is_refused_by_type(self) -> None:
        with self.assertRaises(InputTooLong) as caught:
            embedder().embed(EmbedRequest(texts=["beta " * 5]))
        self.assertEqual((caught.exception.tokens, caught.exception.limit), (7, 6))

    def test_truncation_keeps_the_special_tokens_and_the_chosen_end(self) -> None:
        words = "alpha beta gamma delta memory"
        cases = [
            ("right keeps the head", "right", ["alpha", "beta", "gamma", "delta"]),
            ("left keeps the tail", "left", ["beta", "gamma", "delta", "memory"]),
        ]
        for name, direction, kept in cases:
            with self.subTest(name):
                session = FakeSession()
                engine = embedder(session)
                engine.embed(EmbedRequest(texts=[words], truncate=True, truncation_direction=direction))
                fed = session.calls[0]["input_ids"].tolist()[0]
                self.assertEqual(fed, [VOCAB["[CLS]"], *[VOCAB[w] for w in kept], VOCAB["[SEP]"]])
                self.assertLessEqual(len(fed), OPTIONS["max_input_tokens"])

                with self.assertRaises(InputTooLong, msg="truncation outlived the request"):
                    engine.embed(EmbedRequest(texts=[words]))

    def test_matryoshka_width_and_unit_length(self) -> None:
        vectors = embedder().embed(EmbedRequest(texts=["alpha", "beta"], dimensions=8))
        self.assertEqual(vectors.shape, (2, 8))
        np.testing.assert_allclose(np.linalg.norm(vectors, axis=1), [1.0, 1.0], rtol=1e-6)

    def test_the_inputs_a_graph_asks_for_are_fed(self) -> None:
        session = FakeSession([Input("input_ids", ["b", "l"]), Input("attention_mask", ["b", "t"]),
                               Input("token_type_ids", ["b", "l"]), Input("position_ids", ["b", "l"]),
                               Input("past_key_values.0.key", ["b", 8, "p", 128])])
        embedder(session).embed(EmbedRequest(texts=["alpha beta", "gamma"]))
        feeds = session.calls[0]
        self.assertEqual(sorted(feeds), ["attention_mask", "input_ids", "past_key_values.0.key",
                                         "position_ids", "token_type_ids"])
        np.testing.assert_array_equal(feeds["token_type_ids"], np.zeros((2, 4)))
        np.testing.assert_array_equal(feeds["position_ids"], [[0, 1, 2, 3], [0, 1, 2, 2]])
        self.assertEqual(feeds["past_key_values.0.key"].shape, (2, 8, 0, 128))

    def test_a_graph_or_tokenizer_it_cannot_serve_is_refused_at_load(self) -> None:
        cases = [
            ("an input it cannot feed", {"session": FakeSession([Input("pixel_values", [])])}, "pixel_values"),
            ("a pad token the tokenizer lacks", {"pad_token": "<pad>"}, "pad_token '<pad>'"),
        ]
        for name, change, message in cases:
            with self.subTest(name):
                with self.assertRaises(ValueError) as caught:
                    embedder(**change)
                self.assertIn(message, str(caught.exception))


class AdapterTest(unittest.TestCase):
    def test_dip_infer_is_refused_with_the_way_to_the_vectors(self) -> None:
        engine = OnnxEmbedder.__new__(OnnxEmbedder)
        with self.assertRaises(ValueError) as caught:
            engine.infer(b"text")
        self.assertIn("POST /embed", str(caught.exception))

    def test_a_closed_engine_says_so(self) -> None:
        engine = OnnxEmbedder.__new__(OnnxEmbedder)
        engine._embedder = embedder()  # noqa: SLF001 - no weights, so no real constructor
        engine.close()
        with self.assertRaises(RuntimeError):
            engine.embed(EmbedRequest(texts=["alpha"]))

    def test_the_factory_knows_one_engine(self) -> None:
        self.assertEqual(ENGINE_NAMES, ("onnx_embedder",))
        with self.assertRaises(UnknownEngine):
            build_engine("sentence_transformers", Path("."), {})


if __name__ == "__main__":
    unittest.main()
