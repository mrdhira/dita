"""The pair guard: each registered `body` against its model's own tokenizer.

A cross-encoder's reference implementation feeds the graph `tokenizer(query, passage)`; the
engine encodes one string built from `body`. The two must be the same ids. The guard needs the
real tokenizers, which the suite never downloads, so it runs where they exist: `make parity`.
Everywhere else it is skipped, and says so.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path

from reranker_worker.engines.cross_encoder import DEFAULT_BODY, Reranker, RerankerConfig, RerankRequest
from worker import load_registry, model_dir

from .support import FakeSession

MODELS_ENV = "RERANKER_PARITY_MODELS"
MANIFEST = Path(__file__).resolve().parent.parent / "models.yaml"
QUERY = "what is the red planet?"
TEXTS = [
    "Mars is often called the Red Planet.",
    "  leading and trailing space  ",
    "",
    "Ada kucing di atas meja.\nline two </s> with a literal separator",
    "日本語の文書です。",
]


@unittest.skipUnless(os.environ.get(MODELS_ENV), f"{MODELS_ENV} is not set: no tokenizers here (make parity)")
class PairTest(unittest.TestCase):
    def setUp(self) -> None:
        from tokenizers import Tokenizer

        self.registry = load_registry(MANIFEST)
        self.models = [spec for spec in self.registry.models.values()
                       if spec.options.get("body", DEFAULT_BODY) != DEFAULT_BODY]
        # Every REGISTERED model that sets its own body is under the guard. The count follows the
        # registry rather than a fixed number, because jina-reranker-v2-base-multilingual is a
        # commented entry in models.yaml until memory is retained in Indonesian or Japanese.
        jina = sorted(s.id for s in self.registry.models.values() if s.id.startswith("jina-reranker"))
        self.assertEqual(sorted(s.id for s in self.models), jina,
                         "every registered model with its own body must be guarded")
        self.assertTrue(self.models, "no model with its own body is registered")
        self.tokenizers = {spec.id: Tokenizer.from_file(str(model_dir(Path(os.environ[MODELS_ENV]), spec)
                                                            / spec.options["tokenizer"]))
                           for spec in self.models}

    def reranker(self, model_id: str) -> Reranker:
        options = self.registry.get(model_id).options
        return Reranker(FakeSession(), self.tokenizers[model_id], RerankerConfig.from_options(options))

    def test_the_body_encodes_to_the_tokenizers_own_pair(self) -> None:
        for spec in self.models:
            with self.subTest(spec.id):
                sequences = self.reranker(spec.id).sequences(RerankRequest(query=QUERY, texts=TEXTS))
                reference = [self.tokenizers[spec.id].encode(QUERY, text) for text in TEXTS]
                self.assertEqual(sequences, [encoding.ids for encoding in reference])
                self.assertEqual({t for encoding in reference for t in encoding.type_ids}, {0},
                                 "the engine feeds zero token types; the reference must too")

    def test_truncation_keeps_the_query_and_the_closing_separator(self) -> None:
        for spec in self.models:
            with self.subTest(spec.id):
                engine = self.reranker(spec.id)
                limit = engine.config.max_input_tokens
                text = "Mars is often called the Red Planet. " * limit
                whole = self.tokenizers[spec.id].encode(QUERY, text).ids
                self.assertGreater(len(whole), limit, "the fixture must be too long to fit")
                head = self.tokenizers[spec.id].encode(QUERY, "").ids[:-1]
                for direction in ("right", "left"):
                    request = RerankRequest(query=QUERY, texts=[text], truncate=True,
                                            truncation_direction=direction)
                    (sequence,) = engine.sequences(request)
                    self.assertEqual(len(sequence), limit)
                    self.assertEqual(sequence[:len(head)], head)
                    self.assertEqual(sequence[-1], whole[-1])
                    self.assertNotEqual(sequence[-2], whole[-1])


if __name__ == "__main__":
    unittest.main()
