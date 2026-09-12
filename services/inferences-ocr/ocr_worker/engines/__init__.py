"""Engine adapters, looked up by the `engine` field in models.yaml.

Imports are lazy so that a missing optional dependency only breaks the engine that
needs it, not the whole worker.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from .base import Box, Engine, Line, Result

__all__ = ["Box", "Engine", "Line", "Result", "build_engine", "ENGINE_NAMES"]

ENGINE_NAMES = ("rapidocr", "tesseract", "manga_ocr")


class UnknownEngine(Exception):
    """models.yaml names an engine this worker does not implement."""


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
