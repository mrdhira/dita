"""RapidOCR over PP-OCRv5 ONNX weights, ONNXRuntime only.

Where this engine sits in the pipeline. RapidOCR's package *is* the whole pipeline:
resize and normalise, the DBNet detector, box extraction and unclipping, optional angle
classification, the CRNN recogniser, and the CTC decode -- argmax over the per-timestep
softmax, collapse repeats, drop the blank class, average the kept probabilities into a
line confidence. None of that is ours to write, and reimplementing it would only be a way
to introduce bugs. This adapter is thin on purpose.

So the only knobs we set are the ones with a real reason:

  det_model / rec_model   the two ONNX files our fetcher pinned and verified, instead of
                          letting RapidOCR download its own copies at first use
  rec_keys_from           the label set, because the PaddlePaddle export carries no
                          `character` metadata (see `_materialise_rec_keys`)
  use_cls  = false        the 180-degree angle classifier is a third model file for a case
                          our inputs do not have; RapidOCR's vertical padding covers the
                          rest. Turn it on and add the cls file to models.yaml if that
                          stops being true.
  text_score = 0.5        RapidOCR's own default, stated rather than inherited silently
  log_level  = error      the library logs per-call at info; our own logging is enough

Everything before and after that is the library's. The only real work here is BGR channel
order on the way in, and reshaping RapidOCR's parallel tuples into `Result` on the way out.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import yaml
from PIL import Image

from .base import Engine, Line, Result

LOG = logging.getLogger(__name__)

REC_KEYS_FILENAME = "rec_keys.txt"


class RapidOcrEngine(Engine):
    def __init__(self, model_dir: Path, options: Dict[str, Any]) -> None:
        super().__init__(model_dir, options)

        from rapidocr import EngineType, RapidOCR

        det_model = model_dir / options["det_model"]
        rec_model = model_dir / options["rec_model"]
        params = {
            "Global.use_cls": bool(options.get("use_cls", False)),
            "Global.text_score": float(options.get("text_score", 0.5)),
            "Global.log_level": "error",
            "Det.engine_type": EngineType.ONNXRUNTIME,
            "Det.model_path": str(det_model),
            "Rec.engine_type": EngineType.ONNXRUNTIME,
            "Rec.model_path": str(rec_model),
        }

        rec_keys_from = options.get("rec_keys_from")
        if rec_keys_from:
            params["Rec.rec_keys_path"] = str(_materialise_rec_keys(model_dir / rec_keys_from))

        self._ocr = RapidOCR(params=params)

    def infer(self, image_bytes: bytes) -> Result:
        image = Image.open(io.BytesIO(image_bytes))
        image.load()
        array = np.asarray(image.convert("RGB"))[:, :, ::-1]  # RapidOCR expects BGR

        output = self._ocr(array)
        if output is None or not output.txts:
            return Result(text="", lines=[])

        boxes = output.boxes if output.boxes is not None else [None] * len(output.txts)
        scores = output.scores if output.scores is not None else [None] * len(output.txts)

        lines: List[Line] = []
        for text, score, box in zip(output.txts, scores, boxes):
            lines.append(
                Line(
                    text=text,
                    confidence=None if score is None else round(float(score), 5),
                    box=None if box is None else np.asarray(box).round(1).tolist(),
                )
            )
        return Result(text="\n".join(line.text for line in lines), lines=lines)

    def close(self) -> None:
        self._ocr = None


def _materialise_rec_keys(inference_yml: Path) -> Path:
    """Write the CTC label set next to the model, taking it from the pinned yml.

    The PaddlePaddle ONNX export carries no `character` metadata, which is where RapidOCR
    normally reads the label set from, so we hand it a plain keys file instead. The yml
    is itself checksummed by the fetcher, so the derived file inherits that provenance.
    """
    keys_path = inference_yml.parent / REC_KEYS_FILENAME
    if keys_path.exists():
        return keys_path

    with inference_yml.open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    characters = (config.get("PostProcess") or {}).get("character_dict")
    if not characters:
        raise ValueError(f"{inference_yml} has no PostProcess.character_dict to build {REC_KEYS_FILENAME} from")

    keys_path.write_text("\n".join(characters), encoding="utf-8")
    LOG.info("wrote %s (%d characters) from %s", keys_path, len(characters), inference_yml.name)
    return keys_path
