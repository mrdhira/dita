"""Which ops exist, which fields each one declares, and what refusing looks like.

The layer above framing and below the work: by the time `validate` returns None the op is
known and its fields are well-formed, and what happens next depends on receiver state.

Undeclared fields are refused, never ignored. Silent tolerance hides a typo in a client and
turns a client bug into a server behaviour, so the rule here is the opposite of the one a
requester applies to a response. `specs/dip/conformance/dispatch.json` is the corpus.
"""

from __future__ import annotations

from typing import Any

from .errors import ErrorCode, error

# Fields each op declares besides `op`. `load` is the only op where `model` means anything.
OP_FIELDS: dict[str, frozenset[str]] = {
    "handshake": frozenset(),
    "version": frozenset(),
    "list": frozenset(),
    "load": frozenset({"id", "model"}),
    "unload": frozenset(),
    "infer": frozenset(),
    "livez": frozenset(),
    "readyz": frozenset(),
    "startupz": frozenset(),
}
OPS = tuple(OP_FIELDS)
PROBE_OPS = ("livez", "readyz", "startupz")

INFER_MODEL_HINT = (
    "infer takes no `model` field: `load` the model first and `unload` when done, "
    "so which model answered is never in doubt"
)


def validate(control: dict[str, Any]) -> dict[str, Any] | None:
    """None means dispatch may proceed. Anything else is the refusal to send back."""
    op = control.get("op")
    declared = OP_FIELDS.get(op) if isinstance(op, str) else None

    unexpected = sorted(set(control) - {"op"} - (declared or frozenset()))
    if unexpected and declared is not None:
        # A caller naming a model wants that model; running the resident one would answer
        # a different question, so this refusal points at `load` rather than listing fields.
        if op == "infer" and "model" in unexpected:
            return error(ErrorCode.bad_request, INFER_MODEL_HINT)
        return error(
            ErrorCode.bad_request,
            f"{op} takes no {', '.join(repr(field) for field in unexpected)} field",
        )

    if declared is None:
        return error(ErrorCode.bad_request, f"unknown op {op!r}; expected one of {', '.join(OPS)}")
    if op == "load" and not (control.get("id") or control.get("model")):
        return error(ErrorCode.bad_request, "load needs an `id`")
    return None
