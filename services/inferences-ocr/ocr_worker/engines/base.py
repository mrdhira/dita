"""What every OCR engine adapter has to look like."""

from __future__ import annotations

import abc
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

Box = List[List[float]]  # four [x, y] corners, clockwise from top-left


@dataclass
class Line:
    text: str
    confidence: Optional[float]
    box: Optional[Box]

    def as_dict(self) -> Dict[str, Any]:
        return {"text": self.text, "confidence": self.confidence, "box": self.box}


@dataclass
class Result:
    text: str
    lines: List[Line]

    def as_dict(self) -> Dict[str, Any]:
        return {"text": self.text, "lines": [line.as_dict() for line in self.lines]}


class Engine(abc.ABC):
    """A loaded model. Constructed once by the manager, released on unload.

    Implementations do the expensive work (opening ONNX sessions, reading dictionaries)
    in ``__init__`` so that ``load`` on the wire means "ready to infer".
    """

    def __init__(self, model_dir: Path, options: Dict[str, Any]) -> None:
        self.model_dir = model_dir
        self.options = options

    @abc.abstractmethod
    def infer(self, image_bytes: bytes) -> Result:
        """Run OCR over one encoded image (PNG/JPEG/... bytes)."""

    def close(self) -> None:
        """Release native resources. Safe to call more than once."""
