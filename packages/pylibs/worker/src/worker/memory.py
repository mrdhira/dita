"""Give the memory of a released engine back to the operating system.

glibc keeps freed heap for reuse instead of returning it, so without this an unloaded model's
gigabytes stay in the process and the next load is built on top of them: the budget becomes
the last model plus the next, not the largest. A reload of a 1.2 GB graph was OOM-killed at a
4 GB limit that its first load fitted in comfortably.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import gc
from typing import Any, Optional

_libc: Optional[Any] = None
_looked = False


def trim() -> bool:
    """True when memory was handed back; False where there is no glibc to ask (musl, macOS)."""
    global _libc, _looked
    if not _looked:
        _looked = True
        name = ctypes.util.find_library("c")
        try:
            candidate = ctypes.CDLL(name) if name else None
        except OSError:
            candidate = None
        _libc = candidate if candidate is not None and hasattr(candidate, "malloc_trim") else None
    # A native session can sit in a reference cycle, and only a collection frees it.
    gc.collect()
    if _libc is None:
        return False
    _libc.malloc_trim(0)
    return True
