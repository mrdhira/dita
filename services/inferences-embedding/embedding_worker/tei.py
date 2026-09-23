"""The HuggingFace Text Embeddings Inference surface: `POST /embed`, `GET /info`, `GET /health`.

This is an integration contract, not a convenience: an external memory service speaks TEI to
this port. Request fields, error bodies and status codes follow TEI's router source
(router/src/http/types.rs and server.rs). Where this service is stricter -- an unknown field
is refused, not ignored -- the refusal still has TEI's shape.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

import numpy as np
from textinfer import Admission, TeiError, flag, method_not_allowed, truncation_direction, unhealthy
from worker import Response, Route, json_response

from . import __version__
from .engines.onnx_embedder import EmbedRequest

MAX_CLIENT_BATCH_SIZE = 32
# Below the port's eight connections, so a scrape or /info always has one to use.
MAX_CONCURRENT_REQUESTS = 4
FIELDS = frozenset({"inputs", "normalize", "truncate", "truncation_direction", "prompt_name",
                    "dimensions"})


def routes(max_concurrent: int = MAX_CONCURRENT_REQUESTS) -> Mapping[str, Route]:
    admission = Admission(max_concurrent)

    def embed(manager: Any, method: str, body: bytes) -> Response:
        if method != "POST":
            return method_not_allowed("POST")
        try:
            request = parse_embed(body)
            model_id, vectors, infer_ms = admission.run(manager, lambda engine: engine.embed(request))
        except TeiError as exc:
            return exc.response()
        if not np.isfinite(vectors).all():
            return TeiError("Backend", "the model produced a non-finite value").response()
        return Response(200, encode_vectors(vectors),
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
            "model_dtype": "float32",
            "served_model_name": spec.id,
            "model_type": {"embedding": {"pooling": options["pooling"]}},
            "max_concurrent_requests": max_concurrent,
            "max_input_length": options["max_input_tokens"],
            "max_batch_tokens": options["max_batch_tokens"],
            "max_batch_requests": None,
            "max_client_batch_size": MAX_CLIENT_BATCH_SIZE,
            "auto_truncate": False,
            "tokenization_workers": 1,
            "version": __version__,
            "sha": None,
            "docker_label": None,
            "dimensions": options["dimensions"],
            "prompt_names": sorted(options.get("prompts") or {}),
        })

    def health(manager: Any, method: str, body: bytes) -> Response:
        if method != "GET":
            return method_not_allowed("GET")
        return Response(200, b"") if manager.resident() is not None else unhealthy()

    return {"/embed": embed, "/info": info, "/health": health}


def parse_embed(body: bytes) -> EmbedRequest:
    try:
        raw = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TeiError("Validation", f"the body is not JSON: {exc}") from None
    if not isinstance(raw, dict):
        raise TeiError("Validation", "the body must be a JSON object")
    unknown = sorted(set(raw) - FIELDS)
    if unknown:
        raise TeiError("Validation", f"unknown field(s): {', '.join(unknown)}")
    if "inputs" not in raw:
        raise TeiError("Validation", "missing field `inputs`")

    inputs = raw["inputs"]
    texts = [inputs] if isinstance(inputs, str) else inputs
    if not isinstance(texts, list) or not all(isinstance(text, str) for text in texts):
        raise TeiError("Validation", "`inputs` must be a string or a list of strings; "
                                     "token id inputs are not supported")
    if not texts:
        raise TeiError("Empty", "`inputs` cannot be empty")
    if len(texts) > MAX_CLIENT_BATCH_SIZE:
        raise TeiError("Validation", f"batch size {len(texts)} > maximum allowed batch size "
                                     f"{MAX_CLIENT_BATCH_SIZE}")

    direction = truncation_direction(raw)
    dimensions = raw.get("dimensions")
    if dimensions is not None and (not _int(dimensions) or dimensions <= 0):
        raise TeiError("Validation", "`dimensions` should be positive")
    prompt_name = raw.get("prompt_name")
    if prompt_name is not None and not isinstance(prompt_name, str):
        raise TeiError("Validation", "`prompt_name` must be a string")

    return EmbedRequest(
        texts=texts,
        prompt_name=prompt_name,
        normalize=flag(raw, "normalize", True),
        truncate=flag(raw, "truncate", False),
        truncation_direction=direction,
        dimensions=dimensions,
    )


def encode_vectors(vectors: np.ndarray) -> bytes:
    """float32 at its shortest round-tripping spelling, as TEI sends it. `tolist` would
    widen every value to a 17-digit float64 and double the body for no information."""
    rows = (",".join(map(str, row)) for row in vectors.astype(np.float32))
    return ("[" + ",".join(f"[{row}]" for row in rows) + "]").encode("ascii")


def _int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)
