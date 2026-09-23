"""Text embedding over an ONNX graph: tokenise, batch by a token budget, run, pool, normalise.

One adapter serves every model in models.yaml. What differs between them is data -- pooling,
prompts, limits -- and a model card is honoured or silently broken in exactly that data, so
each step is a pure function the tests can hold to it without weights.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np

from worker import Engine, Result

POOLINGS = ("mean", "last_token", "graph")
THREADS_ENV = "EMBEDDING_THREADS"
EPSILON = 1e-12


class InvalidRequest(ValueError):
    """The caller asked for something this model cannot do: the one failure that is theirs."""


class InputTooLong(InvalidRequest):
    def __init__(self, index: int, tokens: int, limit: int) -> None:
        super().__init__(
            f"`inputs` must have less than {limit} tokens. Given: {tokens} (input {index}); "
            "send `truncate: true` to cut it instead"
        )
        self.index, self.tokens, self.limit = index, tokens, limit


@dataclass(frozen=True)
class EmbedderConfig:
    onnx: str
    tokenizer: str
    pad_token: str
    output: str
    pooling: str
    dimensions: int
    max_input_tokens: int
    max_batch_tokens: int
    prompts: Mapping[str, str] = field(default_factory=dict)
    matryoshka_layer_norm: bool = False
    matryoshka_dimensions: Tuple[int, ...] = ()
    max_sequence_length: Optional[int] = None

    @classmethod
    def from_options(cls, options: Mapping[str, Any]) -> "EmbedderConfig":
        missing = [name for name in ("onnx", "tokenizer", "pad_token", "output", "pooling",
                                     "dimensions", "max_input_tokens", "max_batch_tokens")
                   if name not in options]
        if missing:
            raise ValueError(f"embedding options are missing {', '.join(missing)}")
        if options["pooling"] not in POOLINGS:
            raise ValueError(f"pooling {options['pooling']!r} is not one of {', '.join(POOLINGS)}")
        for name in ("dimensions", "max_input_tokens", "max_batch_tokens"):
            if not _positive_int(options[name]):
                raise ValueError(f"{name} must be a positive integer, got {options[name]!r}")
        if options["max_input_tokens"] > options["max_batch_tokens"]:
            raise ValueError("max_input_tokens cannot exceed max_batch_tokens: one input "
                             "must always fit in one batch")
        card_limit = options.get("max_sequence_length")
        if card_limit is not None and options["max_input_tokens"] > card_limit:
            raise ValueError("max_input_tokens cannot exceed the model's max_sequence_length")
        prompts = dict(options.get("prompts") or {})
        if not all(isinstance(key, str) and isinstance(value, str) for key, value in prompts.items()):
            raise ValueError("prompts must map names to strings")
        return cls(
            onnx=str(options["onnx"]),
            tokenizer=str(options["tokenizer"]),
            pad_token=str(options["pad_token"]),
            output=str(options["output"]),
            pooling=options["pooling"],
            dimensions=options["dimensions"],
            max_input_tokens=options["max_input_tokens"],
            max_batch_tokens=options["max_batch_tokens"],
            prompts=prompts,
            matryoshka_layer_norm=bool(options.get("matryoshka_layer_norm", False)),
            matryoshka_dimensions=tuple(options.get("matryoshka_dimensions") or ()),
            max_sequence_length=card_limit,
        )


@dataclass(frozen=True)
class EmbedRequest:
    texts: Sequence[str]
    prompt_name: Optional[str] = None
    normalize: bool = True
    truncate: bool = False
    truncation_direction: str = "right"
    dimensions: Optional[int] = None


class Embedder:
    """Everything except opening the files, so a test can hand it a fake session and a
    tokenizer built in memory. Not thread-safe: `ModelManager.run` serialises callers."""

    def __init__(self, session: Any, tokenizer: Any, config: EmbedderConfig) -> None:
        self._session = session
        self._tokenizer = tokenizer
        self._config = config
        pad_id = tokenizer.token_to_id(config.pad_token)
        if pad_id is None:
            raise ValueError(f"pad_token {config.pad_token!r} is not in the tokenizer")
        self._pad_id = pad_id
        self._inputs = [(item.name, item.shape) for item in session.get_inputs()]
        unknown = [name for name, _ in self._inputs if not _feedable(name)]
        if unknown:
            raise ValueError(f"the graph wants inputs this adapter cannot feed: {', '.join(unknown)}")
        tokenizer.no_padding()
        tokenizer.no_truncation()

    @property
    def config(self) -> EmbedderConfig:
        return self._config

    def embed(self, request: EmbedRequest) -> np.ndarray:
        prompt = self._prompt(request.prompt_name)
        dimensions = request.dimensions or self._config.dimensions
        if not 0 < dimensions <= self._config.dimensions:
            raise InvalidRequest(
                f"`dimensions` must be between 1 and {self._config.dimensions}, got {dimensions}"
            )

        sequences = self._tokenize([prompt + text for text in request.texts], request)
        pooled = np.zeros((len(sequences), self._config.dimensions), dtype=np.float32)
        for group in plan_batches([len(ids) for ids in sequences], self._config.max_batch_tokens):
            input_ids, mask = pad([sequences[index] for index in group], self._pad_id)
            output = self._session.run([self._config.output], self._feeds(input_ids, mask))[0]
            pooled[group] = pool(self._config.pooling, np.asarray(output, dtype=np.float32), mask)

        return finish(pooled, dimensions, request.normalize,
                      self._config.matryoshka_layer_norm and dimensions < self._config.dimensions)

    def _prompt(self, name: Optional[str]) -> str:
        if name is None:
            return ""
        if name not in self._config.prompts:
            known = ", ".join(sorted(self._config.prompts)) or "none"
            raise InvalidRequest(f"prompt_name {name!r} is not defined for this model; known: {known}")
        return self._config.prompts[name]

    def _tokenize(self, texts: List[str], request: EmbedRequest) -> List[List[int]]:
        limit = self._config.max_input_tokens
        sequences = [encoding.ids for encoding in self._tokenizer.encode_batch(texts)]
        over = [index for index, ids in enumerate(sequences) if len(ids) > limit]
        if over and not request.truncate:
            raise InputTooLong(over[0], len(sequences[over[0]]), limit)
        if over:
            # Truncation through the tokenizer, not a slice, so the special tokens survive.
            self._tokenizer.enable_truncation(limit, direction=request.truncation_direction)
            try:
                cut = self._tokenizer.encode_batch([texts[index] for index in over])
            finally:
                self._tokenizer.no_truncation()
            for index, encoding in zip(over, cut):
                sequences[index] = encoding.ids
        return sequences

    def _feeds(self, input_ids: np.ndarray, mask: np.ndarray) -> Dict[str, np.ndarray]:
        feeds: Dict[str, np.ndarray] = {}
        for name, shape in self._inputs:
            if name == "input_ids":
                feeds[name] = input_ids
            elif name == "attention_mask":
                feeds[name] = mask
            elif name == "token_type_ids":
                feeds[name] = np.zeros_like(input_ids)
            elif name == "position_ids":
                feeds[name] = np.maximum(np.cumsum(mask, axis=1) - 1, 0)
            else:
                # A decoder exported with a KV cache: an empty past is a plain forward pass.
                feeds[name] = np.zeros((input_ids.shape[0], shape[1], 0, shape[3]), dtype=np.float32)
        return feeds


class OnnxEmbedder(Engine):
    def __init__(self, model_dir: Path, options: Dict[str, Any]) -> None:
        super().__init__(model_dir, options)

        import onnxruntime as ort
        from tokenizers import Tokenizer

        config = EmbedderConfig.from_options(options)
        session_options = ort.SessionOptions()
        session_options.log_severity_level = 3
        # The arena keeps every high-water mark: one long batch would pin gigabytes for good.
        session_options.enable_cpu_mem_arena = False
        session_options.intra_op_num_threads = int(os.environ.get(THREADS_ENV, "0") or 0)
        session = ort.InferenceSession(
            str(model_dir / config.onnx), sess_options=session_options,
            providers=["CPUExecutionProvider"],
        )
        tokenizer = Tokenizer.from_file(str(model_dir / config.tokenizer))
        self._embedder: Optional[Embedder] = Embedder(session, tokenizer, config)

    @property
    def config(self) -> EmbedderConfig:
        return self._live().config

    def embed(self, request: EmbedRequest) -> np.ndarray:
        return self._live().embed(request)

    def infer(self, payload: bytes) -> Result:
        raise ValueError("this worker answers with vectors, which DIP's infer response cannot "
                         "carry; POST /embed on the HTTP port instead")

    def close(self) -> None:
        self._embedder = None

    def _live(self) -> Embedder:
        if self._embedder is None:
            raise RuntimeError("the engine was closed")
        return self._embedder


def plan_batches(lengths: Sequence[int], max_batch_tokens: int) -> List[List[int]]:
    """Longest first, so each batch pads to a length its members nearly share, and no batch
    costs more than `max_batch_tokens` once padded. An input always gets a batch."""
    batches: List[List[int]] = []
    for index in sorted(range(len(lengths)), key=lambda i: -lengths[i]):
        current = batches[-1] if batches else None
        if current and (len(current) + 1) * lengths[current[0]] <= max_batch_tokens:
            current.append(index)
        else:
            batches.append([index])
    return batches


def pad(sequences: Sequence[Sequence[int]], pad_id: int) -> Tuple[np.ndarray, np.ndarray]:
    width = max(len(ids) for ids in sequences)
    input_ids = np.full((len(sequences), width), pad_id, dtype=np.int64)
    mask = np.zeros((len(sequences), width), dtype=np.int64)
    for row, ids in enumerate(sequences):
        input_ids[row, : len(ids)] = ids
        mask[row, : len(ids)] = 1
    return input_ids, mask


def pool(strategy: str, output: np.ndarray, mask: np.ndarray) -> np.ndarray:
    if strategy == "graph":
        return output
    if strategy == "mean":
        weights = mask[:, :, None].astype(np.float32)
        return (output * weights).sum(axis=1) / np.maximum(weights.sum(axis=1), EPSILON)
    if strategy == "last_token":
        # The last position the mask marks as real, wherever the padding went.
        last = mask.shape[1] - 1 - np.argmax(mask[:, ::-1], axis=1)
        return output[np.arange(output.shape[0]), last]
    raise ValueError(f"pooling {strategy!r} is not one of {', '.join(POOLINGS)}")


def finish(pooled: np.ndarray, dimensions: int, normalize: bool, layer_norm: bool) -> np.ndarray:
    """Matryoshka then L2, in the order the model cards give: layer-norm first where the
    card asks for it, truncate, and only then normalise the shorter vector."""
    vectors = pooled
    if layer_norm:
        centred = vectors - vectors.mean(axis=1, keepdims=True)
        vectors = centred / np.sqrt((centred**2).mean(axis=1, keepdims=True) + 1e-5)
    vectors = vectors[:, :dimensions]
    if normalize:
        vectors = vectors / np.maximum(np.linalg.norm(vectors, axis=1, keepdims=True), EPSILON)
    return vectors.astype(np.float32)


def _feedable(name: str) -> bool:
    return name in ("input_ids", "attention_mask", "token_type_ids", "position_ids") or (
        name.startswith("past_key_values.")
    )


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0
