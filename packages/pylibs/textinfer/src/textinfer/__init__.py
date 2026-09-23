"""textinfer -- the parts two TEI-compatible text workers share and neither should own.

`batching`: token-budgeted batches for an ONNX graph over tokenised text. `tei`: TEI's error
bodies, status codes and request idioms, and the admission rule every route follows.
"""

from .batching import feedable, feeds, open_session, pad, plan_batches
from .errors import InputTooLong, InvalidRequest
from .tei import (
    STATUS,
    Admission,
    TeiError,
    flag,
    method_not_allowed,
    truncation_direction,
    unhealthy,
)

__all__ = [
    "Admission",
    "InputTooLong",
    "InvalidRequest",
    "STATUS",
    "TeiError",
    "feedable",
    "feeds",
    "flag",
    "method_not_allowed",
    "open_session",
    "pad",
    "plan_batches",
    "truncation_direction",
    "unhealthy",
]
