"""The reranking logic: the prompt around each pair, truncation, grouping and batching.

The ids the graph sees are the whole contract with the model card, so most assertions here
are on exactly those ids.
"""

from __future__ import annotations

import unittest
from pathlib import Path

import numpy as np
from textinfer import InputTooLong, InvalidRequest

from reranker_worker.engines import ENGINE_NAMES, build_engine
from reranker_worker.engines.cross_encoder import DEFAULT_BODY, OnnxCrossEncoder, RerankerConfig, RerankRequest
from worker import UnknownEngine

from .support import OPTIONS, VOCAB, FakeSession, Input, ids, pair_tokenizer, reranker

PREFIX, SUFFIX = ids("judge:"), ids("answer:")
HEAD = ids("<Instruct>: find it <Query>:")
ROBERTA_PAIR = {"prefix": None, "suffix": None, "instruction": None, "body": "{query}</s></s>{text}"}


def pair(query: str, text: str) -> list:
    return PREFIX + HEAD + ids(query) + ids("<Document>:") + ids(text) + SUFFIX


class ConfigTest(unittest.TestCase):
    def test_what_a_config_refuses(self) -> None:
        cases = [
            ("a missing field", {"onnx": None}, "missing onnx"),
            ("a string limit", {"max_input_tokens": "12"}, "max_input_tokens must be a positive"),
            ("a boolean budget", {"max_batch_tokens": True}, "max_batch_tokens must be a positive"),
            ("a pair that cannot fit a batch", {"max_input_tokens": 25}, "cannot exceed max_batch_tokens"),
            ("a limit past the model card", {"max_sequence_length": 8}, "max_sequence_length"),
            ("a prompt that is not text", {"instruction": 3}, "instruction must be a string"),
            ("a body that is not text", {"body": 3}, "body must be a string"),
            ("a body that needs an absent instruction", {"instruction": None},
             "body uses {instruction} but the options set no instruction"),
            ("a body without the query", {"body": "{instruction} {text}"}, "body must contain {query}"),
            ("a body without the document", {"body": "{query}"}, "body must contain {text}"),
            ("a body with the document twice", {"body": "{query} {text} {text}"}, "body must contain {text}"),
            ("an unknown placeholder", {"body": "{title} {query} {text}"}, "body has the placeholder {title}"),
            ("a positional placeholder", {"body": "{} {query} {text}"}, "body has the placeholder {}"),
            ("a converted placeholder", {"body": "{query!r} {text}"}, "body has the placeholder {query!r}"),
            ("a formatted placeholder", {"body": "{query:>9} {text}"}, "body has the placeholder {query:>9}"),
            ("a broken template", {"body": "{query {text}"}, "body is not a valid template"),
        ]
        for name, change, message in cases:
            with self.subTest(name):
                options = {key: value for key, value in {**OPTIONS, **change}.items() if value is not None}
                with self.assertRaises(ValueError) as caught:
                    RerankerConfig.from_options(options)
                self.assertIn(message, str(caught.exception))

    def test_what_a_config_defaults(self) -> None:
        config = RerankerConfig.from_options(OPTIONS)
        self.assertEqual(config.body, DEFAULT_BODY)
        self.assertIn("{instruction}", config.body)
        bare = RerankerConfig.from_options({k: v for k, v in OPTIONS.items()
                                            if k not in ("prefix", "suffix", "instruction")}
                                           | {"body": "{query} {text}"})
        self.assertEqual((bare.prefix, bare.suffix, bare.instruction), ("", "", ""))

    def test_auto_truncate_is_off_unless_the_manifest_says_so(self) -> None:
        self.assertFalse(RerankerConfig.from_options(OPTIONS).auto_truncate)
        self.assertTrue(RerankerConfig.from_options({**OPTIONS, "auto_truncate": True}).auto_truncate)


class SequenceTest(unittest.TestCase):
    def test_each_pair_is_prefix_instruction_query_document_suffix(self) -> None:
        sequences = reranker().sequences(RerankRequest(query="mars", texts=["red planet", "noise"]))
        self.assertEqual(sequences, [pair("mars", "red planet"), pair("mars", "noise")])

    def test_an_explicit_default_body_changes_nothing(self) -> None:
        request = RerankRequest(query="mars", texts=["red planet", "noise"])
        self.assertEqual(reranker(body=DEFAULT_BODY).sequences(request), reranker().sequences(request))

    def test_a_custom_body_is_used_verbatim(self) -> None:
        cases = [
            ("without the prompt", {"prefix": None, "suffix": None, "instruction": None,
                                    "body": "<Query>: {query} <Document>: {text}"},
             ids("<Query>: mars <Document>: red planet")),
            ("inside the prompt, with the instruction twice", {"body": "{instruction} {text} {query} {instruction}"},
             PREFIX + ids("find it red planet mars find it") + SUFFIX),
        ]
        for name, change, expected in cases:
            with self.subTest(name):
                (sequence,) = reranker(**change).sequences(RerankRequest(query="mars", texts=["red planet"]))
                self.assertEqual(sequence, expected)

    def test_a_roberta_body_is_the_tokenizers_own_pair(self) -> None:
        built = pair_tokenizer()
        texts = ["red planet", "noise", ""]
        sequences = reranker(built=built, **ROBERTA_PAIR).sequences(RerankRequest(query="mars", texts=texts))
        self.assertEqual(sequences, [built.encode("mars", text).ids for text in texts])
        self.assertEqual(sequences[0], [VOCAB[t] for t in ("<s>", "mars", "</s>", "</s>", "red", "planet", "</s>")])

    def test_truncation_keeps_what_follows_the_document(self) -> None:
        text = "red planet relevant noise mars " * 3
        limit = OPTIONS["max_input_tokens"]
        opening, closing = [VOCAB["<s>"]] + ids("mars") + [VOCAB["</s>"]] * 2, [VOCAB["</s>"]]
        room = limit - len(opening) - len(closing)
        after = ids("<Query>: mars")
        self.assertGreater(len(ids(text)), limit, "the fixture must be too long to fit")
        cases = [
            ("a closing special token, cut right", {"built": pair_tokenizer(), **ROBERTA_PAIR}, "right",
             opening + ids(text)[:room] + closing),
            ("a closing special token, cut left", {"built": pair_tokenizer(), **ROBERTA_PAIR}, "left",
             opening + ids(text)[-room:] + closing),
            ("the query after the document", {"prefix": None, "suffix": None, "instruction": None,
                                              "body": "{text} <Query>: {query}"}, "right",
             ids(text)[:limit - len(after)] + after),
        ]
        for name, change, direction, expected in cases:
            with self.subTest(name):
                request = RerankRequest(query="mars", texts=[text], truncate=True,
                                        truncation_direction=direction)
                (sequence,) = reranker(**change).sequences(request)
                self.assertEqual(sequence, expected)
                self.assertEqual(len(sequence), limit)

    def test_truncation_cuts_only_the_document(self) -> None:
        text = "red planet relevant noise mars red"
        budget = OPTIONS["max_input_tokens"] - len(PREFIX) - len(SUFFIX)
        room = budget - len(HEAD) - len(ids("mars <Document>:"))
        self.assertGreater(len(ids(text)), room, "the fixture must be too long to fit")
        cases = [
            ("right keeps the head of the document", "right", ids(text)[:room]),
            ("left keeps the tail of the document", "left", ids(text)[-room:]),
        ]
        for name, direction, kept in cases:
            with self.subTest(name):
                request = RerankRequest(query="mars", texts=[text], truncate=True,
                                        truncation_direction=direction)
                (sequence,) = reranker().sequences(request)
                self.assertEqual(sequence, PREFIX + HEAD + ids("mars <Document>:") + kept + SUFFIX)
                self.assertEqual(len(sequence), OPTIONS["max_input_tokens"])

    def test_a_pair_exactly_at_the_limit_is_kept_whole(self) -> None:
        text = "red planet relevant noise"
        self.assertEqual(len(pair("mars", text)), OPTIONS["max_input_tokens"])
        self.assertEqual(reranker().sequences(RerankRequest(query="mars", texts=[text])),
                         [pair("mars", text)])

    def test_what_a_request_is_refused_for(self) -> None:
        over = "red planet relevant noise mars"
        self.assertEqual(len(pair("mars", over)), OPTIONS["max_input_tokens"] + 1)
        long_query = "mars red planet it find"
        self.assertGreaterEqual(len(pair(long_query, "")), OPTIONS["max_input_tokens"])
        cases = [
            ("a pair past the limit without truncate",
             RerankRequest(query="mars", texts=["red", over]), InputTooLong, "Given: 13 (input 1)"),
            ("a query that leaves the document no room",
             RerankRequest(query=long_query, texts=["red"], truncate=True), InvalidRequest,
             "the query alone"),
        ]
        for name, request, error, message in cases:
            with self.subTest(name):
                with self.assertRaises(error) as caught:
                    reranker().sequences(request)
                self.assertIn(message, str(caught.exception))


class RankTest(unittest.TestCase):
    def test_one_logit_per_text_in_request_order_across_batches(self) -> None:
        session = FakeSession()
        texts = ["noise", "relevant relevant", "red", "relevant noise noise"]
        logits = reranker(session).rank(RerankRequest(query="mars", texts=texts))

        self.assertGreater(len(session.calls), 1, "the budget should have split this request")
        np.testing.assert_array_equal(logits, [-1.0, 2.0, 0.0, -1.0])
        self.assertEqual(logits.dtype, np.float32)

    def test_padding_never_changes_a_score(self) -> None:
        alone = reranker().rank(RerankRequest(query="mars", texts=["relevant"]))
        beside_a_longer_one = reranker().rank(RerankRequest(query="mars", texts=["relevant", "red planet"]))
        self.assertEqual(alone[0], beside_a_longer_one[0])

    def test_the_graph_is_fed_right_padded_rows_and_their_mask(self) -> None:
        session = FakeSession()
        reranker(session).rank(RerankRequest(query="mars", texts=["red planet", "red"]))
        feeds = session.calls[0]
        self.assertEqual(sorted(feeds), ["attention_mask", "input_ids"])
        self.assertEqual(feeds["input_ids"][1].tolist(), pair("mars", "red") + [VOCAB["<pad>"]])
        self.assertEqual(feeds["attention_mask"].tolist(), [[1] * 10, [1] * 9 + [0]])

    def test_a_graph_or_tokenizer_it_cannot_serve_is_refused_at_load(self) -> None:
        cases = [
            ("an input it cannot feed", {"session": FakeSession([Input("pixel_values", [])])}, "pixel_values"),
            ("a pad token the tokenizer lacks", {"pad_token": "<|endoftext|>"}, "pad_token"),
            ("a prompt that fills the budget", {"prefix": "judge: " * 11}, "prompt alone"),
        ]
        for name, change, message in cases:
            with self.subTest(name):
                with self.assertRaises(ValueError) as caught:
                    reranker(**change)
                self.assertIn(message, str(caught.exception))


class AdapterTest(unittest.TestCase):
    def test_dip_infer_is_refused_with_the_way_to_the_scores(self) -> None:
        with self.assertRaises(ValueError) as caught:
            OnnxCrossEncoder.__new__(OnnxCrossEncoder).infer(b"text")
        self.assertIn("POST /rerank", str(caught.exception))

    def test_a_closed_engine_says_so(self) -> None:
        engine = OnnxCrossEncoder.__new__(OnnxCrossEncoder)
        engine._reranker = reranker()  # noqa: SLF001 - no weights, so no real constructor
        self.assertEqual(engine.config.max_input_tokens, OPTIONS["max_input_tokens"])
        engine.close()
        with self.assertRaises(RuntimeError):
            engine.rank(RerankRequest(query="mars", texts=["red"]))

    def test_the_factory_knows_one_engine(self) -> None:
        self.assertEqual(ENGINE_NAMES, ("onnx_cross_encoder",))
        with self.assertRaises(UnknownEngine):
            build_engine("sentence_transformers", Path("."), {})


if __name__ == "__main__":
    unittest.main()
