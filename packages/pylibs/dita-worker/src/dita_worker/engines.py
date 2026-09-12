"""What every engine adapter has to look like, and how the framework asks for one.

The framework never names an engine. A service supplies `build_engine`, and the only thing
declared here is the shape both sides agree on: a factory takes a name from models.yaml and
returns something with `infer` and `close`.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

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
    def infer(self, payload: bytes) -> Result:
        """Run the model over one encoded input (PNG/JPEG/WAV/... bytes)."""

    def close(self) -> None:
        """Release native resources. Safe to call more than once."""


class UnknownEngine(Exception):
    """models.yaml names an engine the service does not implement.

    Raised by a service's factory, not here: the framework knows the name it was given and
    nothing else. `dispatch` turns it into `unsupported_engine`.
    """


# engine name from models.yaml, the model's directory, its options -> a built engine.
EngineFactory = Callable[[str, Path, Dict[str, Any]], Engine]
