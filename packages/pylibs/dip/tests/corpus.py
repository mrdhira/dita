"""Where the conformance corpus lives, and how to read a case out of it.

The corpus is in `specs/`, under neither language, so it is found by walking up from this
file rather than by a path that assumes where the repo is checked out.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CORPUS_DIRNAME = Path("specs") / "dip" / "conformance"


def corpus_dir() -> Path:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / CORPUS_DIRNAME
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError(f"no {CORPUS_DIRNAME} above {__file__}")


def load(name: str) -> dict[str, Any]:
    return json.loads((corpus_dir() / name).read_text(encoding="utf-8"))
