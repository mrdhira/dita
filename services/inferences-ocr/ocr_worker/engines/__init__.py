"""Engine adapters, looked up by the `engine` field in models.yaml.

Imports are lazy so that a missing optional dependency only breaks the engine that
needs it, not the whole worker.

`build_engine` and `ENGINE_NAMES` are the two things `worker` cannot know: the
framework is handed this factory at startup and never names an engine itself.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from worker import Engine, UnknownEngine

__all__ = ["ENGINE_NAMES", "build_engine"]

ENGINE_NAMES = ("rapidocr", "tesseract", "manga_ocr")


def build_engine(name: str, model_dir: Path, options: Dict[str, Any]) -> Engine:
    if name == "rapidocr":
        from .rapidocr_engine import RapidOcrEngine

        return RapidOcrEngine(model_dir, options)
    if name == "tesseract":
        from .tesseract_engine import TesseractEngine

        return TesseractEngine(model_dir, options)
    if name == "manga_ocr":
        from .manga_ocr_engine import MangaOcrEngine

        return MangaOcrEngine(model_dir, options)
    raise UnknownEngine(f"engine '{name}' is not implemented; known engines: {', '.join(ENGINE_NAMES)}")
