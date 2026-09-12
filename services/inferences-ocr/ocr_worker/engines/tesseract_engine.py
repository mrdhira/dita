"""The system tesseract binary, driven through its TSV output.

Why the binary and not a library. Tesseract is a C++ program, not a Python package.
`pytesseract` is a thin wrapper that shells out to this same binary and re-parses the same
TSV, so it would add a dependency and buy nothing: we need the TSV anyway, because that is
the only output carrying per-word boxes and confidences.

Where this engine sits in the pipeline. The binary owns everything -- binarisation, layout
analysis, line finding, recognition, and its own confidence scores. There is no
preprocessing to do on our side and no logits to soften; the only thing this adapter does
after the fact is fold tesseract's one-row-per-word TSV back into lines and reshape it into
the common `Result`. Contrast with `manga_ocr_engine`, which ships a bare graph and leaves
the whole pipeline to us.

On quality. Good on clean printed Latin text. Poor on Japanese, measured: for an image
reading `日本語のテキスト認識` it returns `AA 告 の テキ ス ト 認識`, where PP-OCRv5 is exact.
It stays in the registry as a cheap, dependency-free baseline, never as the default.
"""

from __future__ import annotations

import csv
import io
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List

from .base import Engine, Line, Result

TIMEOUT_SECONDS = 120


class TesseractEngine(Engine):
    def __init__(self, model_dir: Path, options: Dict[str, Any]) -> None:
        super().__init__(model_dir, options)

        self._binary = shutil.which(options.get("binary", "tesseract"))
        if not self._binary:
            raise RuntimeError(
                "the tesseract binary is not on PATH; install the apt packages listed under "
                "source.apt_packages for this model in models.yaml"
            )
        self._lang = str(options.get("lang", "eng"))
        self._psm = str(options.get("psm", 3))

        available = self._installed_languages()
        missing = [code for code in self._lang.split("+") if code not in available]
        if missing:
            raise RuntimeError(
                f"tesseract is installed but is missing language data for {', '.join(missing)}"
            )

    def infer(self, image_bytes: bytes) -> Result:
        completed = subprocess.run(
            [self._binary, "stdin", "stdout", "-l", self._lang, "--psm", self._psm, "tsv"],
            input=image_bytes,
            capture_output=True,
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", "replace").strip()
            raise RuntimeError(f"tesseract exited {completed.returncode}: {detail}")

        return _parse_tsv(completed.stdout.decode("utf-8", "replace"))

    def _installed_languages(self) -> List[str]:
        completed = subprocess.run(
            [self._binary, "--list-langs"], capture_output=True, timeout=TIMEOUT_SECONDS, check=False
        )
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", "replace").strip()
            raise RuntimeError(
                f"`{self._binary} --list-langs` exited {completed.returncode}: {detail}"
            )
        output = completed.stdout.decode("utf-8", "replace").splitlines()
        # The first line is a "List of available languages ..." banner.
        return [row.strip() for row in output[1:] if row.strip()]


def _parse_tsv(tsv: str) -> Result:
    """Fold tesseract's per-word rows into one Line per text line."""
    reader = csv.DictReader(io.StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE)

    grouped: Dict[tuple, Dict[str, Any]] = {}
    for row in reader:
        text = (row.get("text") or "").strip()
        if not text:
            continue
        try:
            confidence = float(row["conf"])
            left, top = int(row["left"]), int(row["top"])
            width, height = int(row["width"]), int(row["height"])
        except (KeyError, TypeError, ValueError):
            continue
        if confidence < 0:
            continue

        key = (row.get("page_num"), row.get("block_num"), row.get("par_num"), row.get("line_num"))
        entry = grouped.setdefault(
            key, {"words": [], "confidences": [], "x0": left, "y0": top, "x1": left, "y1": top}
        )
        entry["words"].append(text)
        entry["confidences"].append(confidence)
        entry["x0"] = min(entry["x0"], left)
        entry["y0"] = min(entry["y0"], top)
        entry["x1"] = max(entry["x1"], left + width)
        entry["y1"] = max(entry["y1"], top + height)

    lines: List[Line] = []
    for entry in grouped.values():
        # Japanese output comes back as separate glyph "words" with no spaces between
        # them; joining on a space would corrupt it, so only space out latin runs.
        joined = " ".join(entry["words"])
        if not any(word.isascii() for word in entry["words"]):
            joined = "".join(entry["words"])
        mean_confidence = sum(entry["confidences"]) / len(entry["confidences"]) / 100.0
        lines.append(
            Line(
                text=joined,
                confidence=round(mean_confidence, 5),
                box=[
                    [float(entry["x0"]), float(entry["y0"])],
                    [float(entry["x1"]), float(entry["y0"])],
                    [float(entry["x1"]), float(entry["y1"])],
                    [float(entry["x0"]), float(entry["y1"])],
                ],
            )
        )

    return Result(text="\n".join(line.text for line in lines), lines=lines)
