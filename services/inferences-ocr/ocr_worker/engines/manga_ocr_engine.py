"""manga-ocr as two ONNX graphs plus a greedy decode loop.

Upstream's pipeline is torch, which this service will not depend on, so preprocessing, the
decode loop and line assembly are ours. The exported decoder has no key/value cache, so each
step re-runs it over the whole prefix. One text block per call: give it a crop, not a page.
"""

from __future__ import annotations

import io
import json
import re
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
from PIL import Image

from worker import Engine, Line, Result

SPECIAL_TOKENS = ("[PAD]", "[UNK]", "[CLS]", "[SEP]", "[MASK]")


class MangaOcrEngine(Engine):
    def __init__(self, model_dir: Path, options: Dict[str, Any]) -> None:
        super().__init__(model_dir, options)

        import onnxruntime as ort

        session_options = ort.SessionOptions()
        session_options.log_severity_level = 3
        providers = ["CPUExecutionProvider"]

        self._encoder = ort.InferenceSession(
            str(model_dir / options["encoder"]), sess_options=session_options, providers=providers
        )
        self._decoder = ort.InferenceSession(
            str(model_dir / options["decoder"]), sess_options=session_options, providers=providers
        )

        self._vocab = (model_dir / options["vocab"]).read_text(encoding="utf-8").splitlines()
        preprocess = _read_json(model_dir / "preprocessor_config.json")
        generation = _read_json(model_dir / "generation_config.json")

        size = preprocess.get("size") or {}
        self._image_size = (int(size.get("width", 224)), int(size.get("height", 224)))
        self._mean = np.asarray(preprocess.get("image_mean", [0.5, 0.5, 0.5]), dtype=np.float32)
        self._std = np.asarray(preprocess.get("image_std", [0.5, 0.5, 0.5]), dtype=np.float32)
        self._rescale = float(preprocess.get("rescale_factor", 1 / 255))

        self._start_token = int(generation.get("decoder_start_token_id", 2))
        self._eos_token = int(generation.get("eos_token_id", 3))
        self._max_tokens = int(options.get("max_tokens", generation.get("max_length", 300)))

    def infer(self, image_bytes: bytes) -> Result:
        pixel_values = self._preprocess(image_bytes)
        encoded = self._encoder.run(None, {"pixel_values": pixel_values})[0]

        token_ids: List[int] = [self._start_token]
        confidences: List[float] = []
        for _ in range(self._max_tokens):
            logits = self._decoder.run(
                None,
                {
                    "input_ids": np.asarray([token_ids], dtype=np.int64),
                    "encoder_hidden_states": encoded,
                },
            )[0]
            step = logits[0, -1]
            next_id = int(step.argmax())
            if next_id == self._eos_token:
                break
            confidences.append(float(_softmax_max(step)))
            token_ids.append(next_id)

        text = _post_process("".join(self._vocab[i] for i in token_ids[1:] if self._is_text(i)))
        if not text:
            return Result(text="", lines=[])

        confidence = round(sum(confidences) / len(confidences), 5) if confidences else None
        return Result(text=text, lines=[Line(text=text, confidence=confidence, box=None)])

    def close(self) -> None:
        self._encoder = None
        self._decoder = None

    def _is_text(self, token_id: int) -> bool:
        return 0 <= token_id < len(self._vocab) and self._vocab[token_id] not in SPECIAL_TOKENS

    def _preprocess(self, image_bytes: bytes) -> np.ndarray:
        image = Image.open(io.BytesIO(image_bytes))
        image.load()
        # Upstream manga-ocr greyscales first, then feeds three identical channels.
        image = image.convert("L").convert("RGB").resize(self._image_size, Image.BILINEAR)

        array = np.asarray(image, dtype=np.float32) * self._rescale
        array = (array - self._mean) / self._std
        return np.expand_dims(array.transpose(2, 0, 1), axis=0)


def _softmax_max(logits: np.ndarray) -> float:
    shifted = logits - logits.max()
    exponentials = np.exp(shifted)
    return float(exponentials.max() / exponentials.sum())


def _post_process(text: str) -> str:
    """The whitespace and ellipsis tidy-up upstream manga-ocr applies. Upstream also runs
    jaconv half-to-full-width conversion; left out to keep the dependency set down."""
    text = "".join(text.split())
    text = text.replace("…", "...")
    return re.sub(r"[・.]{2,}", lambda match: "." * (match.end() - match.start()), text)


def _read_json(path: Path) -> Dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)
