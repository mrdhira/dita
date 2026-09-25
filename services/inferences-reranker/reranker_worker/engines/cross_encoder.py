"""A cross-encoder over an ONNX graph: each (query, text) pair is read together and scored.

The graph is Qwen3-Reranker converted to a one-logit sequence classifier: its logit is the
original model's yes-logit minus its no-logit at the last token, so `sigmoid(logit)` is the
official P(yes). The prompt around each pair is the model card's; the tokens must match the
reference exactly, and a test holds them to it. Another model sets its own `body`: the pair as
one string, which must encode to the ids its reference feeds the graph.
"""

from __future__ import annotations

import os
import string
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np
from textinfer import InputTooLong, InvalidRequest, feedable, feeds, open_session, pad, plan_batches
from worker import Engine, Result

THREADS_ENV = "RERANKER_THREADS"
DEFAULT_BODY = "<Instruct>: {instruction}\n<Query>: {query}\n<Document>: {text}"
PLACEHOLDERS = ("query", "text", "instruction")


def body_fields(body: str) -> List[Tuple[str, Optional[str]]]:
    """The (literal, placeholder) runs of a body, refused unless every placeholder is a bare
    {query}, {text} or {instruction} and the first two appear exactly once."""
    try:
        parsed = list(string.Formatter().parse(body))
    except ValueError as exc:
        raise ValueError(f"body is not a valid template: {exc}") from None
    for _, field, spec, conversion in parsed:
        if field is not None and (field not in PLACEHOLDERS or spec or conversion):
            shown = field + (f"!{conversion}" if conversion else "") + (f":{spec}" if spec else "")
            raise ValueError(f"body has the placeholder {{{shown}}}; only {{query}}, {{text}} "
                             "and {instruction} are allowed")
    fields = [field for _, field, _, _ in parsed if field is not None]
    for name in ("query", "text"):
        if fields.count(name) != 1:
            raise ValueError(f"body must contain {{{name}}} exactly once")
    return [(literal, field) for literal, field, _, _ in parsed]


def split_body(body: str) -> Tuple[str, str]:
    """The templates before and after {text}, so the document's span in a pair is known."""
    parts: Tuple[List[str], List[str]] = ([], [])
    side = 0
    for literal, field in body_fields(body):
        parts[side].append(literal.replace("{", "{{").replace("}", "}}"))
        if field == "text":
            side = 1
        elif field is not None:
            parts[side].append(f"{{{field}}}")
    return "".join(parts[0]), "".join(parts[1])


@dataclass(frozen=True)
class RerankerConfig:
    onnx: str
    tokenizer: str
    pad_token: str
    output: str
    max_input_tokens: int
    max_batch_tokens: int
    prefix: str = ""
    suffix: str = ""
    instruction: str = ""
    body: str = DEFAULT_BODY
    auto_truncate: bool = False
    max_sequence_length: Optional[int] = None

    @classmethod
    def from_options(cls, options: Mapping[str, Any]) -> "RerankerConfig":
        required = ("onnx", "tokenizer", "pad_token", "output", "max_input_tokens", "max_batch_tokens")
        missing = [name for name in required if name not in options]
        if missing:
            raise ValueError(f"reranker options are missing {', '.join(missing)}")
        for name in ("max_input_tokens", "max_batch_tokens"):
            value = options[name]
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer, got {value!r}")
        if options["max_input_tokens"] > options["max_batch_tokens"]:
            raise ValueError("max_input_tokens cannot exceed max_batch_tokens: one pair must "
                             "always fit in one batch")
        card_limit = options.get("max_sequence_length")
        if card_limit is not None and options["max_input_tokens"] > card_limit:
            raise ValueError("max_input_tokens cannot exceed the model's max_sequence_length")
        for name in ("prefix", "suffix", "instruction", "body"):
            if not isinstance(options.get(name, ""), str):
                raise ValueError(f"{name} must be a string")
        body = options.get("body", DEFAULT_BODY)
        if any(field == "instruction" for _, field in body_fields(body)) and "instruction" not in options:
            raise ValueError("body uses {instruction} but the options set no instruction")
        return cls(
            onnx=str(options["onnx"]),
            tokenizer=str(options["tokenizer"]),
            pad_token=str(options["pad_token"]),
            output=str(options["output"]),
            max_input_tokens=options["max_input_tokens"],
            max_batch_tokens=options["max_batch_tokens"],
            prefix=options.get("prefix", ""),
            suffix=options.get("suffix", ""),
            instruction=options.get("instruction", ""),
            body=body,
            auto_truncate=bool(options.get("auto_truncate", False)),
            max_sequence_length=card_limit,
        )


@dataclass(frozen=True)
class RerankRequest:
    query: str
    texts: Sequence[str]
    truncate: bool = False
    truncation_direction: str = "right"


class Reranker:
    """Everything except opening the files, so a test can hand it a fake session and a
    tokenizer built in memory. Not thread-safe: `ModelManager.run` serialises callers."""

    def __init__(self, session: Any, tokenizer: Any, config: RerankerConfig) -> None:
        self._session = session
        self._tokenizer = tokenizer
        self._config = config
        pad_id = tokenizer.token_to_id(config.pad_token)
        if pad_id is None:
            raise ValueError(f"pad_token {config.pad_token!r} is not in the tokenizer")
        self._pad_id = pad_id
        self._inputs = [(item.name, item.shape) for item in session.get_inputs()]
        unknown = [name for name, _ in self._inputs if not feedable(name)]
        if unknown:
            raise ValueError(f"the graph wants inputs this adapter cannot feed: {', '.join(unknown)}")
        tokenizer.no_padding()
        tokenizer.no_truncation()
        self._prefix = tokenizer.encode(config.prefix, add_special_tokens=False).ids
        self._suffix = tokenizer.encode(config.suffix, add_special_tokens=False).ids
        self._before, self._after = split_body(config.body)
        if len(self._prefix) + len(self._suffix) >= config.max_input_tokens:
            raise ValueError("the prompt alone fills max_input_tokens")

    @property
    def config(self) -> RerankerConfig:
        return self._config

    def sequences(self, request: RerankRequest) -> List[List[int]]:
        """The exact ids the graph sees for each pair, in request order."""
        budget = self._config.max_input_tokens - len(self._prefix) - len(self._suffix)
        sequences = []
        for index, text in enumerate(request.texts):
            fill = {"instruction": self._config.instruction, "query": request.query}
            before = self._before.format(**fill)
            body = before + text + self._after.format(**fill)
            encoding = self._tokenizer.encode(body)
            ids = encoding.ids
            if len(ids) > budget:
                if not request.truncate:
                    raise InputTooLong(index, len(ids) + len(self._prefix) + len(self._suffix),
                                       self._config.max_input_tokens)
                ids = self._cut_document(encoding, len(before), len(before) + len(text), budget,
                                         request.truncation_direction)
            sequences.append(self._prefix + ids + self._suffix)
        return sequences

    def rank(self, request: RerankRequest) -> np.ndarray:
        """One raw logit per text, in request order."""
        sequences = self.sequences(request)
        logits = np.zeros(len(sequences), dtype=np.float32)
        for group in plan_batches([len(ids) for ids in sequences], self._config.max_batch_tokens):
            input_ids, mask = pad([sequences[index] for index in group], self._pad_id)
            output = self._session.run([self._config.output], feeds(self._inputs, input_ids, mask))[0]
            logits[group] = np.asarray(output, dtype=np.float32).reshape(len(group), -1)[:, 0]
        return logits

    @staticmethod
    def _cut_document(encoding: Any, start: int, end: int, budget: int, direction: str) -> List[int]:
        """Only the document is cut, so the instruction, the query and any closing special
        token (which the post-processor adds at offset 0) always survive."""
        inside = [i for i, ((first, last), special) in
                  enumerate(zip(encoding.offsets, encoding.special_tokens_mask))
                  if not special and first >= start and last <= end]
        head = inside[0] if inside else len(encoding.ids)
        tail = inside[-1] + 1 if inside else len(encoding.ids)
        room = budget - head - (len(encoding.ids) - tail)
        if room <= 0:
            raise InvalidRequest("the query alone fills max_input_tokens; shorten the query")
        document = encoding.ids[head:tail]
        kept = document[:room] if direction == "right" else document[-room:]
        return encoding.ids[:head] + kept + encoding.ids[tail:]


class OnnxCrossEncoder(Engine):
    def __init__(self, model_dir: Path, options: Dict[str, Any]) -> None:
        super().__init__(model_dir, options)

        from tokenizers import Tokenizer

        config = RerankerConfig.from_options(options)
        session = open_session(model_dir / config.onnx, int(os.environ.get(THREADS_ENV, "0") or 0))
        tokenizer = Tokenizer.from_file(str(model_dir / config.tokenizer))
        self._reranker: Optional[Reranker] = Reranker(session, tokenizer, config)

    @property
    def config(self) -> RerankerConfig:
        return self._live().config

    def rank(self, request: RerankRequest) -> np.ndarray:
        return self._live().rank(request)

    def infer(self, payload: bytes) -> Result:
        raise ValueError("this worker answers with scores, which DIP's infer response cannot "
                         "carry; POST /rerank on the HTTP port instead")

    def close(self) -> None:
        self._reranker = None

    def _live(self) -> Reranker:
        if self._reranker is None:
            raise RuntimeError("the engine was closed")
        return self._reranker
