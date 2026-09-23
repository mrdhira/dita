"""The HuggingFace Text Embeddings Inference reranking surface: `POST /rerank`, `GET /info`,
`GET /health`.

The integration contract for an external memory service. Fields, defaults, ordering and
error codes follow TEI's router source (router/src/http/types.rs `RerankRequest` and `Rank`,
server.rs `rerank`, core/src/infer.rs `predict`). Stricter in one way only: an unknown field
is refused, not ignored.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping

import numpy as np
from textinfer import Admission, TeiError, flag, method_not_allowed, truncation_direction, unhealthy
from worker import Response, Route, json_response

from . import __version__
from .engines.cross_encoder import RerankRequest

MAX_CLIENT_BATCH_SIZE = 32
# Below the port's eight connections, so a scrape or /info always has one to use.
MAX_CONCURRENT_REQUESTS = 4
FIELDS = frozenset({"query", "texts", "truncate", "truncation_direction", "raw_scores",
                    "return_text"})


def routes(max_concurrent: int = MAX_CONCURRENT_REQUESTS) -> Mapping[str, Route]:
    admission = Admission(max_concurrent)

    def rerank(manager: Any, method: str, body: bytes) -> Response:
        if method != "POST":
            return method_not_allowed("POST")
        try:
            raw = _object(body)
            auto_truncate = _auto_truncate(manager)
            request = parse_rerank(raw, auto_truncate)
            model_id, logits, infer_ms = admission.run(manager, lambda engine: engine.rank(request))
            ranks = rank(logits, request.texts, flag(raw, "raw_scores", False),
                         flag(raw, "return_text", False))
        except TeiError as exc:
            return exc.response()
        return Response(200, json.dumps(ranks).encode("utf-8"),
                        headers=(("x-model-id", model_id), ("x-compute-time", str(infer_ms))))

    def info(manager: Any, method: str, body: bytes) -> Response:
        if method != "GET":
            return method_not_allowed("GET")
        resident = manager.resident()
        if resident is None:
            return unhealthy()
        spec = manager.registry.get(resident["id"])
        options = spec.options
        return json_response(200, {
            "model_id": spec.id,
            "model_sha": spec.files[0].revision if spec.files else None,
            "model_dtype": "float16",
            "served_model_name": spec.id,
            "model_type": {"reranker": {"id2label": {"0": "LABEL_0"}, "label2id": {"LABEL_0": 0}}},
            "max_concurrent_requests": max_concurrent,
            "max_input_length": options["max_input_tokens"],
            "max_batch_tokens": options["max_batch_tokens"],
            "max_batch_requests": None,
            "max_client_batch_size": MAX_CLIENT_BATCH_SIZE,
            "auto_truncate": bool(options.get("auto_truncate", False)),
            "tokenization_workers": 1,
            "version": __version__,
            "sha": None,
            "docker_label": None,
        })

    def health(manager: Any, method: str, body: bytes) -> Response:
        if method != "GET":
            return method_not_allowed("GET")
        return Response(200, b"") if manager.resident() is not None else unhealthy()

    return {"/rerank": rerank, "/info": info, "/health": health}


def parse_rerank(raw: Dict[str, Any], auto_truncate: bool) -> RerankRequest:
    unknown = sorted(set(raw) - FIELDS)
    if unknown:
        raise TeiError("Validation", f"unknown field(s): {', '.join(unknown)}")
    for name in ("query", "texts"):
        if name not in raw:
            raise TeiError("Validation", f"missing field `{name}`")
    if not isinstance(raw["query"], str):
        raise TeiError("Validation", "`query` must be a string")
    texts = raw["texts"]
    if not isinstance(texts, list) or not all(isinstance(text, str) for text in texts):
        raise TeiError("Validation", "`texts` must be a list of strings")
    if not texts:
        raise TeiError("Empty", "`texts` cannot be empty")
    if len(texts) > MAX_CLIENT_BATCH_SIZE:
        raise TeiError("Validation", f"batch size {len(texts)} > maximum allowed batch size "
                                     f"{MAX_CLIENT_BATCH_SIZE}")
    flag(raw, "raw_scores", False)
    flag(raw, "return_text", False)
    return RerankRequest(
        query=raw["query"],
        texts=texts,
        truncate=flag(raw, "truncate", auto_truncate),
        truncation_direction=truncation_direction(raw),
    )


def rank(logits: np.ndarray, texts: List[str], raw_scores: bool, return_text: bool) -> List[Dict[str, Any]]:
    """TEI's response: every text once, best first. A score is `sigmoid(logit)` unless the
    caller asked for the logit; ties keep request order."""
    if np.isnan(logits).any():
        raise TeiError("Backend", "score is NaN")
    scores = logits if raw_scores else 1.0 / (1.0 + np.exp(-logits.astype(np.float64)))
    ranks = []
    for index in sorted(range(len(texts)), key=lambda i: (-scores[i], i)):
        entry: Dict[str, Any] = {"index": index, "score": float(str(np.float32(scores[index])))}
        if return_text:
            entry["text"] = texts[index]
        ranks.append(entry)
    return ranks


def _object(body: bytes) -> Dict[str, Any]:
    try:
        raw = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TeiError("Validation", f"the body is not JSON: {exc}") from None
    if not isinstance(raw, dict):
        raise TeiError("Validation", "the body must be a JSON object")
    return raw


def _auto_truncate(manager: Any) -> bool:
    resident = manager.resident()
    if resident is None:
        return False
    return bool(manager.registry.get(resident["id"]).options.get("auto_truncate", False))
