"""Op dispatch: which ops exist and which fields each declares. Undeclared fields are
refused, never ignored -- silent tolerance turns a client typo into a server behaviour.
`specs/dip/conformance/dispatch.json` is the corpus.
"""

from __future__ import annotations

from typing import Any

from .errors import ErrorCode, error

# `load` is the only op where `model` means anything.
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
        # A caller naming a model wants that model, so the refusal points at `load`.
        if op == "infer" and "model" in unexpected:
            return error(ErrorCode.bad_request, INFER_MODEL_HINT)
        return error(
            ErrorCode.bad_request,
            f"{op} takes no {', '.join(repr(field) for field in unexpected)} field",
        )

    if declared is None:
        return error(ErrorCode.bad_request, f"unknown op {op!r}; expected one of {', '.join(OPS)}")
    if op == "load":
        return _load_target_refusal(control)
    return None


def _load_target_refusal(control: dict[str, Any]) -> dict[str, Any] | None:
    """`load` needs one string: an id becomes a directory name and a registry key."""
    for field in ("id", "model"):
        value = control.get(field)
        if value is not None and not isinstance(value, str):
            return error(
                ErrorCode.bad_request,
                f"load `{field}` must be a string, got {type(value).__name__}",
            )
    if not (control.get("id") or control.get("model")):
        return error(ErrorCode.bad_request, "load needs an `id`")
    return None
