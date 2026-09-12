"""The two shapes every response has: `code` is the contract, `message` is prose."""

from __future__ import annotations

from typing import Any

from .types import ErrorCode

__all__ = ["ErrorCode", "error", "ok"]


def ok(**fields: Any) -> dict[str, Any]:
    return {"ok": True, **fields}


def error(code: ErrorCode | str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": str(code), "message": message}}
