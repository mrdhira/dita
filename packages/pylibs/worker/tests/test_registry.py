"""The models.yaml parser: what it accepts, and what it must refuse.

A table, one row per malformed manifest: every one of these becomes a directory name or a
download URL, so the parser is the only place that can stop them.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from worker import RegistryError, load_registry

from .support import registry_in


class RegistryTest(unittest.TestCase):
    MALFORMED = [
        (
            "an id that escapes the models directory",
            "models:\n  - id: ../../escape\n    engine: echo\n"
            "    source: {type: system}\n    files: []\n",
            "safe directory name",
        ),
        (
            "an id with a path separator",
            "models:\n  - id: a/b\n    engine: echo\n"
            "    source: {type: system}\n    files: []\n",
            "safe directory name",
        ),
        (
            "a missing engine",
            "models:\n  - id: fine\n    source: {type: system}\n    files: []\n",
            "missing 'engine'",
        ),
        (
            "a source type nobody implements",
            "models:\n  - id: fine\n    engine: echo\n"
            "    source: {type: carrier-pigeon}\n    files: []\n",
            "expected huggingface or system",
        ),
        (
            "a huggingface source with no files",
            "models:\n  - id: fine\n    engine: echo\n"
            "    source: {type: huggingface, repo: a/b, revision: abc}\n    files: []\n",
            "lists no files",
        ),
        (
            "a file with no pinned revision",
            "models:\n  - id: fine\n    engine: echo\n"
            "    source: {type: huggingface, repo: a/b}\n"
            "    files:\n      - path: w.onnx\n",
            "immutable revision",
        ),
        (
            "a dest that climbs out of the model directory",
            "models:\n  - id: fine\n    engine: echo\n"
            "    source: {type: huggingface, repo: a/b, revision: abc}\n"
            "    files:\n      - {path: w.onnx, dest: ../../w.onnx}\n",
            "must stay inside",
        ),
        (
            "two models sharing an id",
            "models:\n  - id: twin\n    engine: echo\n"
            "    source: {type: system}\n    files: []\n"
            "  - id: twin\n    engine: echo\n"
            "    source: {type: system}\n    files: []\n",
            "duplicate model id",
        ),
        (
            "a default_model that is not in the list",
            "default_model: ghost\nmodels:\n  - id: real\n    engine: echo\n"
            "    source: {type: system}\n    files: []\n",
            "is not one of the declared models",
        ),
        (
            "no models at all",
            "models: []\n",
            "declares no models",
        ),
    ]

    def test_a_malformed_registry_is_rejected_with_a_useful_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for name, body, fragment in self.MALFORMED:
                with self.subTest(name):
                    path = Path(tmp) / "models.yaml"
                    path.write_text("version: 1\n" + body, encoding="utf-8")
                    with self.assertRaises(RegistryError) as caught:
                        load_registry(path)
                    self.assertIn(fragment, str(caught.exception))

    def test_a_missing_registry_file_is_named(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RegistryError) as caught:
                load_registry(Path(tmp) / "absent.yaml")
            self.assertIn("not found", str(caught.exception))

    def test_unknown_model_is_named_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RegistryError) as caught:
                registry_in(Path(tmp)).get("nope")
        message = str(caught.exception)
        self.assertIn("unknown model 'nope'", message)
        self.assertIn("alpha, beta, gamma", message)
