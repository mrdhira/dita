"""A real tokenizer built in memory and a fake ONNX graph: the two seams the engine has.

The graph's logit for a pair is (count of `relevant`) - (count of `noise`) over the tokens it
was fed, so a test can say which text must rank first without weights, and can see exactly
which ids reached the graph.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Sequence

import numpy as np
from tokenizers import Tokenizer, models, pre_tokenizers

from reranker_worker.engines.cross_encoder import Reranker, RerankerConfig, RerankRequest
from worker import Engine, Result

WORDS = ["judge:", "answer:", "<Instruct>:", "<Query>:", "<Document>:", "find", "it", "relevant",
         "noise", "mars", "red", "planet"]
VOCAB = {"<pad>": 0, "<unk>": 1, **{w: i + 2 for i, w in enumerate(WORDS)}}

OPTIONS = {
    "onnx": "onnx/model.onnx",
    "tokenizer": "tokenizer.json",
    "pad_token": "<pad>",
    "output": "logits",
    "prefix": "judge:",
    "suffix": " answer:",
    "instruction": "find it",
    "max_input_tokens": 12,
    "max_batch_tokens": 24,
}


def tokenizer() -> Tokenizer:
    built = Tokenizer(models.WordLevel(VOCAB, unk_token="<unk>"))
    built.pre_tokenizer = pre_tokenizers.WhitespaceSplit()
    return built


def ids(text: str) -> List[int]:
    return [VOCAB.get(word, VOCAB["<unk>"]) for word in text.split()]


@dataclass
class Input:
    name: str
    shape: List[Any]


class FakeSession:
    def __init__(self, inputs: Sequence[Input] = (Input("input_ids", ["b", "l"]),
                                                  Input("attention_mask", ["b", "l"]))) -> None:
        self._inputs = list(inputs)
        self.calls: List[Dict[str, np.ndarray]] = []

    def get_inputs(self) -> List[Input]:
        return self._inputs

    def run(self, names: List[str], feeds: Dict[str, np.ndarray]) -> List[np.ndarray]:
        self.calls.append(feeds)
        tokens, mask = feeds["input_ids"], feeds["attention_mask"].astype(bool)
        score = ((tokens == VOCAB["relevant"]) & mask).sum(1) - ((tokens == VOCAB["noise"]) & mask).sum(1)
        return [score.astype(np.float16).reshape(-1, 1)]


def reranker(session: Any = None, **overrides: Any) -> Reranker:
    return Reranker(session or FakeSession(), tokenizer(),
                    RerankerConfig.from_options({**OPTIONS, **overrides}))


class FakeRerankerEngine(Engine):
    """An engine as `ModelManager` sees it, with a real `Reranker` inside and hooks for the
    failures a route has to translate."""

    gate: threading.Event | None = None
    entered: threading.Event | None = None
    fail_with: BaseException | None = None
    poison: bool = False

    def __init__(self, model_dir: Path, options: Dict[str, Any]) -> None:
        super().__init__(model_dir, options)
        self._reranker = reranker()

    def rank(self, request: RerankRequest) -> np.ndarray:
        cls = type(self)
        if cls.entered is not None:
            cls.entered.set()
        if cls.gate is not None:
            cls.gate.wait(timeout=10)
        if cls.fail_with is not None:
            raise cls.fail_with
        logits = self._reranker.rank(request)
        if cls.poison:
            logits[0] = np.nan
        return logits

    def infer(self, payload: bytes) -> Result:
        raise ValueError("scores only")

    @classmethod
    def reset(cls) -> None:
        cls.gate = cls.entered = cls.fail_with = None
        cls.poison = False


REGISTRY_YAML = """\
version: 1
default_model: fake-reranker
models:
  - id: fake-reranker
    description: an in-memory cross-encoder
    engine: onnx_cross_encoder
    langs: [en]
    source: {type: system}
    options: {onnx: x, tokenizer: y, pad_token: "<pad>", output: logits, prefix: "judge:",
              suffix: " answer:", instruction: "find it", max_input_tokens: 12,
              max_batch_tokens: 24, auto_truncate: true}
"""
