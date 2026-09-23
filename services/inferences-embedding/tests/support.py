"""A real tokenizer built in memory and a fake ONNX session: the two seams the engine has.

The session's hidden state is a function of the token ids it was fed, so a test can say what
a vector must be without weights, and can see exactly which ids and inputs reached the graph.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Sequence

import numpy as np
from tokenizers import Tokenizer, models, pre_tokenizers, processors

from embedding_worker.engines.onnx_embedder import Embedder, EmbedderConfig, EmbedRequest
from worker import Engine, Result

WORDS = ["search_query:", "search_document:", "alpha", "beta", "gamma", "delta", "memory"]
VOCAB = {"[PAD]": 0, "[UNK]": 1, "[CLS]": 2, "[SEP]": 3, **{w: i + 4 for i, w in enumerate(WORDS)}}
WIDTH = 16

OPTIONS = {
    "onnx": "onnx/model.onnx",
    "tokenizer": "tokenizer.json",
    "pad_token": "[PAD]",
    "output": "last_hidden_state",
    "pooling": "mean",
    "dimensions": WIDTH,
    "max_input_tokens": 6,
    "max_batch_tokens": 12,
    "prompts": {"query": "search_query: ", "document": "search_document: "},
}


def tokenizer() -> Tokenizer:
    built = Tokenizer(models.WordLevel(VOCAB, unk_token="[UNK]"))
    built.pre_tokenizer = pre_tokenizers.WhitespaceSplit()
    built.post_processor = processors.TemplateProcessing(
        single="[CLS] $A [SEP]", special_tokens=[("[CLS]", 2), ("[SEP]", 3)]
    )
    return built


@dataclass
class Input:
    name: str
    shape: List[Any]


class FakeSession:
    """Token id t at position p contributes a one-hot at t and p/100 at the last column, so
    mean pooling, last-token pooling and padding leaks all show up in the numbers."""

    def __init__(self, inputs: Sequence[Input] = (Input("input_ids", ["b", "l"]),
                                                  Input("attention_mask", ["b", "l"]))) -> None:
        self._inputs = list(inputs)
        self.calls: List[Dict[str, np.ndarray]] = []

    def get_inputs(self) -> List[Input]:
        return self._inputs

    def run(self, names: List[str], feeds: Dict[str, np.ndarray]) -> List[np.ndarray]:
        self.calls.append(feeds)
        ids = feeds["input_ids"]
        hidden = np.zeros(ids.shape + (WIDTH,), dtype=np.float32)
        for row in range(ids.shape[0]):
            for column in range(ids.shape[1]):
                hidden[row, column, ids[row, column] % (WIDTH - 1)] = 1.0
                hidden[row, column, WIDTH - 1] = column / 100
        return [hidden]


def embedder(session: Any = None, **overrides: Any) -> Embedder:
    return Embedder(session or FakeSession(), tokenizer(),
                    EmbedderConfig.from_options({**OPTIONS, **overrides}))


class FakeEmbeddingEngine(Engine):
    """An engine as `ModelManager` sees it, with a real `Embedder` inside and hooks for the
    failures a route has to translate."""

    gate: threading.Event | None = None
    entered: threading.Event | None = None
    fail_with: BaseException | None = None
    poison: bool = False

    def __init__(self, model_dir: Path, options: Dict[str, Any]) -> None:
        super().__init__(model_dir, options)
        self._embedder = embedder()

    def embed(self, request: EmbedRequest) -> np.ndarray:
        cls = type(self)
        if cls.entered is not None:
            cls.entered.set()
        if cls.gate is not None:
            cls.gate.wait(timeout=10)
        if cls.fail_with is not None:
            raise cls.fail_with
        vectors = self._embedder.embed(request)
        if cls.poison:
            vectors[0, 0] = np.nan
        return vectors

    def infer(self, payload: bytes) -> Result:
        raise ValueError("vectors only")

    @classmethod
    def reset(cls) -> None:
        cls.gate = cls.entered = cls.fail_with = None
        cls.poison = False


REGISTRY_YAML = f"""\
version: 1
default_model: fake-embedder
models:
  - id: fake-embedder
    description: an in-memory embedder
    engine: onnx_embedder
    langs: [en]
    source: {{type: system}}
    options: {{onnx: x, tokenizer: y, pad_token: "[PAD]", output: last_hidden_state,
               pooling: mean, dimensions: {WIDTH}, max_input_tokens: 6, max_batch_tokens: 12,
               prompts: {{query: "search_query: "}}}}
"""
