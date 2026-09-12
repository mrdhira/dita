"""The two shapes every response has, and the taxonomy of the failing one.

`code` is the contract and `message` is prose: a caller branches on the code and logs the
message. ErrorCode is re-exported from the generated types rather than written again here,
because a taxonomy spelled out twice is a taxonomy that drifts from the IDL.
"""

from __future__ import annotations

from typing import Any

from .types import ErrorCode

__all__ = ["ErrorCode", "error", "ok"]


def ok(**fields: Any) -> dict[str, Any]:
    return {"ok": True, **fields}


def error(code: ErrorCode | str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": str(code), "message": message}}
