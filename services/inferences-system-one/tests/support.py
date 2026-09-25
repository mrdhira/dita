"""A real tokenizer built in memory, a fake encoder graph and small random head weights: the
three seams the engine has.

The fake encoder is a per-token lookup (the token's embedding plus its position's), so nothing
mixes tokens except the head under test. That is what lets a test say padding must not move an
answer and mean it.
"""

from __future__ import annotations

import json
import struct
import threading
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence

import numpy as np
from tokenizers import Tokenizer, models, pre_tokenizers

from system_one_worker.engines.decision import Decider, Decision, OnnxDecision, Special
from worker import Engine, Result

# Two heads of 64, as upstream splits its head (d // 64), so the multi-head path is exercised.
D = 128
HEADS = 2
WORDS = ["choice", "score", "noul", "question:", "false:", "true:", "no,", "yes,", "the", "statement",
         "does", "not", "hold", "holds", "level", "0:", "1:", "2:", "3:", "severity", "info", "warning",
         "critical", "oom", "restart", "disk", "ok", "is", "it", "down", "needs", "human"]
SPECIALS = ["<pad>", "<unk>", "<bos>", "<eos>", "<mask>"]
VOCAB = {w: i for i, w in enumerate(SPECIALS + WORDS)}
SPECIAL = Special("<mask>", VOCAB["<mask>"], VOCAB["<bos>"], VOCAB["<eos>"], VOCAB["<pad>"])
TOKENIZER_CONFIG = {"mask_token": "<mask>", "cls_token": "<bos>", "sep_token": "<eos>", "pad_token": "<pad>"}
CFG = {"head_layers": 2, "max_len": 64, "head_max_len": 32, "temperature": [1.0, 1.0, 1.0],
       "temperature_by_options": {}}


def tokenizer() -> Tokenizer:
    built = Tokenizer(models.WordLevel(VOCAB, unk_token="<unk>"))
    built.pre_tokenizer = pre_tokenizers.WhitespaceSplit()
    return built


def head_weights(seed: int = 7, d: int = D, layers: int = 2) -> Dict[str, np.ndarray]:
    rng = np.random.default_rng(seed)
    r = lambda *shape: (rng.standard_normal(shape) * 0.2).astype(np.float32)
    w = {"type_emb.weight": r(3, d), "scorer.0.weight": 1 + r(d), "scorer.0.bias": r(d),
         "scorer.1.weight": r(d, d), "scorer.1.bias": r(d), "scorer.3.weight": r(1, d), "scorer.3.bias": r(1),
         "act_head.0.weight": r(256, d + 4), "act_head.0.bias": r(256), "act_head.2.weight": r(2, 256),
         "act_head.2.bias": r(2)}
    for i in range(layers):
        p = "head.layers.%d." % i
        w.update({p + "self_attn.in_proj_weight": r(3 * d, d), p + "self_attn.in_proj_bias": r(3 * d),
                  p + "self_attn.out_proj.weight": r(d, d), p + "self_attn.out_proj.bias": r(d),
                  p + "linear1.weight": r(4 * d, d), p + "linear1.bias": r(4 * d),
                  p + "linear2.weight": r(d, 4 * d), p + "linear2.bias": r(d),
                  p + "norm1.weight": 1 + r(d), p + "norm1.bias": r(d),
                  p + "norm2.weight": 1 + r(d), p + "norm2.bias": r(d)})
    return w


class FakeSession:
    """last_hidden_state = a fixed embedding per token id and per position, as a real encoder's
    marker rows differ by position; records every feed it was given."""

    def __init__(self, seed: int = 3) -> None:
        rng = np.random.default_rng(seed)
        self.table = rng.standard_normal((len(VOCAB), D)).astype(np.float32)
        self.positions = rng.standard_normal((CFG["max_len"], D)).astype(np.float32)
        self.calls: List[Dict[str, np.ndarray]] = []

    def run(self, names: Any, feeds: Dict[str, np.ndarray]) -> List[np.ndarray]:
        self.calls.append(feeds)
        ids = feeds["input_ids"]
        return [self.table[ids] + self.positions[: ids.shape[1]]]


def decider(session: Any = None, cfg: Mapping[str, Any] = CFG, max_batch_tokens: int = 128,
            weights: Mapping[str, np.ndarray] | None = None, heads: int = HEADS) -> Decider:
    return Decider(session or FakeSession(), tokenizer(), SPECIAL, weights or head_weights(), cfg,
                   max_batch_tokens, heads)


def write_safetensors(path: Path, tensors: Mapping[str, np.ndarray], dtypes: Mapping[str, str] | None = None) -> None:
    """The safetensors layout (u64 header length, JSON header, raw little-endian data)."""
    names = {"F16": np.float16, "F32": np.float32, "BF16": np.uint16}
    header, blobs, offset = {"__metadata__": {"format": "pt"}}, [], 0
    for key, value in tensors.items():
        dtype = (dtypes or {}).get(key, "F32")
        raw = np.ascontiguousarray(value.astype(names[dtype])).tobytes()
        header[key] = {"dtype": dtype, "shape": list(value.shape), "data_offsets": [offset, offset + len(raw)]}
        blobs.append(raw)
        offset += len(raw)
    encoded = json.dumps(header).encode()
    path.write_bytes(struct.pack("<Q", len(encoded)) + encoded + b"".join(blobs))


class FakeDecisionEngine(Engine):
    """An engine as `ModelManager` sees it, with a real `Decider` inside and hooks for the
    failures a route has to translate."""

    gate: threading.Event | None = None
    entered: threading.Event | None = None
    fail_with: BaseException | None = None

    def __init__(self, model_dir: Path, options: Dict[str, Any]) -> None:
        super().__init__(model_dir, options)
        self._decider = decider()

    def decide(self, state: Any, questions: Sequence[Mapping[str, Any]], deadline: float | None = None,
               max_tokens: int | None = None) -> List[Decision]:
        cls = type(self)
        if cls.entered is not None:
            cls.entered.set()
        if cls.gate is not None:
            cls.gate.wait(timeout=10)
        if cls.fail_with is not None:
            raise cls.fail_with
        return self._decider.decide(state, questions, deadline, max_tokens)

    def infer(self, payload: bytes) -> Result:
        return OnnxDecision.infer(self, payload)

    @classmethod
    def reset(cls) -> None:
        cls.gate = cls.entered = cls.fail_with = None

