"""HuggingFace Text Embeddings Inference's error contract, and the admission rule.

Error bodies, status codes and field idioms follow TEI's router source
(router/src/http/types.rs, server.rs). Admission is the same on every route: parse before
taking a slot, refuse past the cap rather than queue, never load a model on a request.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Dict, Tuple, TypeVar

from worker import NoModelLoaded, Response, json_response

from .errors import InvalidRequest

LOG = logging.getLogger(__name__)

STATUS = {"Unhealthy": 503, "Backend": 424, "Overloaded": 429, "Tokenizer": 422,
          "Validation": 422, "Empty": 400}

T = TypeVar("T")


class TeiError(Exception):
    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type

    def response(self) -> Response:
        headers = (("Retry-After", "1"),) if self.error_type == "Overloaded" else ()
        return json_response(STATUS[self.error_type],
                             {"error": str(self), "error_type": self.error_type}, headers)


class Admission:
    """At most `limit` requests inside the model at once; the rest are told 429 at once."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._slots = threading.BoundedSemaphore(limit)

    def run(self, manager: Any, work: Callable[[Any], T]) -> Tuple[str, T, float]:
        """`manager.run(work)`, with every failure already a `TeiError`."""
        if not self._slots.acquire(blocking=False):
            raise TeiError("Overloaded", "Model is overloaded")
        try:
            return manager.run(work)
        except NoModelLoaded as exc:
            raise TeiError("Unhealthy", str(exc)) from None
        except InvalidRequest as exc:
            raise TeiError("Validation", str(exc)) from None
        except Exception as exc:  # noqa: BLE001 - reported to the caller as TEI does
            LOG.exception("inference failed")
            raise TeiError("Backend", f"{type(exc).__name__}: {exc}") from None
        finally:
            self._slots.release()


def flag(raw: Dict[str, Any], name: str, default: bool) -> bool:
    value = raw.get(name)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise TeiError("Validation", f"`{name}` must be a boolean")
    return value


def truncation_direction(raw: Dict[str, Any]) -> str:
    """TEI accepts `left`/`Left` and `right`/`Right`."""
    direction = raw.get("truncation_direction", "right")
    if not isinstance(direction, str) or direction.lower() not in ("left", "right"):
        raise TeiError("Validation", "`truncation_direction` must be `left` or `right`")
    return direction.lower()


def unhealthy() -> Response:
    return TeiError("Unhealthy", "no model is loaded; the orchestrator has not loaded one "
                                 "yet").response()


def method_not_allowed(allowed: str) -> Response:
    return json_response(405, {"error": f"use {allowed}", "error_type": "Validation"},
                         (("Allow", allowed),))
