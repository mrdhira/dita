"""Stdlib-only tests for the OCR half of this service: the three engine adapters, the
manifest they are selected by, and the identity this service hands `dita_worker`.

The framework -- the registry parser, the fetcher, the manager, the socket server, the
health probes and the framing -- is tested in `packages/pylibs/dita-worker`, because that
is where it lives. What is left here is what would still be true if the wire changed.

    python -m unittest discover -s tests -t .
"""

from __future__ import annotations

import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict
from unittest import mock

import numpy as np
from PIL import Image

from dita_worker import ModelManager, Result, UnknownEngine, dispatch, load_registry

from ocr_worker import __version__
from ocr_worker.__main__ import REGISTRY_PATH, WORKER, main
from ocr_worker.engines import ENGINE_NAMES, build_engine
from ocr_worker.engines.manga_ocr_engine import MangaOcrEngine
from ocr_worker.engines.rapidocr_engine import RapidOcrEngine, _materialise_rec_keys
from ocr_worker.engines import tesseract_engine
from ocr_worker.engines.tesseract_engine import TesseractEngine, _parse_tsv

FIXTURES = Path(__file__).resolve().parent / "fixtures"

MODEL_IDS = ["rapidocr-ppocrv5", "tesseract", "manga-ocr"]


class RegistryTest(unittest.TestCase):
    """The manifest this service ships. What a malformed manifest does is the parser's
    problem, and is tested where the parser lives."""

    def test_shipped_registry_parses(self) -> None:
        registry = load_registry(REGISTRY_PATH)
        self.assertEqual(registry.default_model, "rapidocr-ppocrv5")
        self.assertEqual(set(registry.models), set(MODEL_IDS))

    def test_every_downloadable_file_has_a_pinned_digest(self) -> None:
        for spec in load_registry(REGISTRY_PATH).models.values():
            for spec_file in spec.files:
                self.assertTrue(spec_file.verified, f"{spec.id}/{spec_file.dest} has no sha256")
                self.assertEqual(len(spec_file.sha256), 64)


class ServiceIdentityTest(unittest.TestCase):
    """The wiring: what this service tells `dita_worker` about itself.

    `dispatch` itself is the framework's, and is tested there with a fake worker. These
    assert the arguments *this* service supplies, because a wrong name or a stale engine
    tuple is a wire-visible bug that no OCR test would catch.
    """

    def setUp(self) -> None:
        self.manager = ModelManager(
            load_registry(REGISTRY_PATH), Path("/nonexistent"), WORKER.build_engine
        )

    def test_the_handshake_advertises_this_service_and_its_engines(self) -> None:
        response = dispatch(WORKER, self.manager, {"op": "handshake"}, b"")
        self.assertTrue(response["ok"])
        self.assertEqual(response["service"], "inferences-ocr")
        self.assertEqual(response["version"], __version__)
        self.assertEqual(response["engines"], list(ENGINE_NAMES))
        self.assertEqual(response["default_model"], "rapidocr-ppocrv5")

    def test_every_engine_named_in_the_manifest_has_an_adapter(self) -> None:
        listing = dispatch(WORKER, self.manager, {"op": "list"}, b"")
        self.assertEqual([model["id"] for model in listing["models"]], MODEL_IDS)
        for model in listing["models"]:
            with self.subTest(model["id"]):
                self.assertIn(model["engine"], ENGINE_NAMES)

    def test_the_defaults_the_image_and_compose_file_rely_on(self) -> None:
        """Both are derived from the name now; the Dockerfile and compose still hard-code
        the old strings, so a rename has to break here rather than at runtime."""
        self.assertEqual(WORKER.default_socket_path, "/run/dita/inferences-ocr.sock")
        self.assertEqual(WORKER.registry_path, Path(__file__).resolve().parent.parent / "models.yaml")
        self.assertIs(WORKER.build_engine, build_engine)

    def test_the_module_entrypoint_hands_this_worker_to_the_framework(self) -> None:
        """`python -m ocr_worker` is one delegating line. Exit 2 is the framework's answer
        to a registry it cannot read, so reaching it proves the hand-off happened."""
        with mock.patch("logging.basicConfig"), self.assertLogs("dita_worker.cli", "ERROR"):
            self.assertEqual(main(["--registry", str(REGISTRY_PATH.parent / "absent.yaml")]), 2)



# ---------------------------------------------------------------------------
# Engine adapters. These are the OCR logic itself, and they are pure: no model
# files, no network, no tesseract binary. Each is a table -- one row per case,
# subTest(name) so a failure names the row and a new case is one line.
# ---------------------------------------------------------------------------

TSV_COLUMNS = (
    "level page_num block_num par_num line_num word_num left top width height conf text"
)


def tsv(*rows: str) -> str:
    """Build a tesseract TSV from readable space-separated rows.

    The real thing is tab-separated with those twelve columns; splitting on the first
    eleven spaces keeps a text field that itself contains spaces intact.
    """
    header = "\t".join(TSV_COLUMNS.split())
    body = ["\t".join(row.split(" ", 11)) for row in rows]
    return "\n".join([header, *body]) + "\n"


def _completed(returncode: int, stdout: bytes = b"", stderr: bytes = b""):
    """A stand-in for subprocess.CompletedProcess."""
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def shape(result: Result) -> list:
    """The part of a Result worth asserting: (text, confidence, box) per line."""
    return [(line.text, line.confidence, line.box) for line in result.lines]


def box(x0: float, y0: float, x1: float, y1: float) -> list:
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


class TesseractTsvTest(unittest.TestCase):
    """Folding tesseract's one-row-per-word TSV back into lines.

    Nothing here runs tesseract: `_parse_tsv` is the whole of what this service owns for
    that engine, so it is the whole of what there is to unit test.
    """

    CASES = [
        {
            "name": "one word becomes one line",
            "tsv": tsv("5 1 1 1 1 1 10 20 30 12 96.0 Hello"),
            "text": "Hello",
            "lines": [("Hello", 0.96, box(10.0, 20.0, 40.0, 32.0))],
        },
        {
            "name": "words on one line are space-joined and the box is their union",
            "tsv": tsv(
                "5 1 1 1 1 1 10 20 30 12 90.0 The",
                "5 1 1 1 1 2 50 18 40 16 100.0 quick",
            ),
            "text": "The quick",
            "lines": [("The quick", 0.95, box(10.0, 18.0, 90.0, 34.0))],
        },
        {
            "name": "two line_nums in one block become two lines",
            "tsv": tsv(
                "5 1 1 1 1 1 10 20 30 12 90.0 first",
                "5 1 1 1 2 1 10 40 30 12 80.0 second",
            ),
            "text": "first\nsecond",
            "lines": [
                ("first", 0.9, box(10.0, 20.0, 40.0, 32.0)),
                ("second", 0.8, box(10.0, 40.0, 40.0, 52.0)),
            ],
        },
        {
            "name": "two blocks stay separate even at the same line_num",
            "tsv": tsv(
                "5 1 1 1 1 1 10 20 30 12 90.0 blockone",
                "5 1 2 1 1 1 10 60 30 12 90.0 blocktwo",
            ),
            "text": "blockone\nblocktwo",
            "lines": [
                ("blockone", 0.9, box(10.0, 20.0, 40.0, 32.0)),
                ("blocktwo", 0.9, box(10.0, 60.0, 40.0, 72.0)),
            ],
        },
        {
            "name": "structural rows carry no text and are skipped",
            "tsv": tsv(
                "1 1 0 0 0 0 0 0 900 300 -1 ",
                "4 1 1 1 1 0 10 20 30 12 -1 ",
                "5 1 1 1 1 1 10 20 30 12 90.0 word",
            ),
            "text": "word",
            "lines": [("word", 0.9, box(10.0, 20.0, 40.0, 32.0))],
        },
        {
            "name": "a negative confidence is dropped, not averaged in",
            "tsv": tsv(
                "5 1 1 1 1 1 10 20 30 12 90.0 kept",
                "5 1 1 1 1 2 50 20 30 12 -1 rejected",
            ),
            "text": "kept",
            "lines": [("kept", 0.9, box(10.0, 20.0, 40.0, 32.0))],
        },
        {
            "name": "a whitespace-only text field is dropped",
            "tsv": tsv(
                "5 1 1 1 1 1 10 20 30 12 90.0 kept",
                "5 1 1 1 1 2 50 20 30 12 95.0    ",
            ),
            "text": "kept",
            "lines": [("kept", 0.9, box(10.0, 20.0, 40.0, 32.0))],
        },
        {
            "name": "a row with a non-numeric geometry is skipped rather than crashing",
            "tsv": tsv(
                "5 1 1 1 1 1 10 20 30 12 90.0 kept",
                "5 1 1 1 1 2 nope 20 30 12 90.0 broken",
            ),
            "text": "kept",
            "lines": [("kept", 0.9, box(10.0, 20.0, 40.0, 32.0))],
        },
        {
            "name": "japanese glyph words are joined without spaces",
            "tsv": tsv(
                "5 1 1 1 1 1 10 20 30 30 90.0 日本",
                "5 1 1 1 1 2 40 20 30 30 90.0 語",
            ),
            "text": "日本語",
            "lines": [("日本語", 0.9, box(10.0, 20.0, 70.0, 50.0))],
        },
        {
            "name": "a line with any ascii word keeps the spaces",
            "tsv": tsv(
                "5 1 1 1 1 1 10 20 30 30 90.0 ID",
                "5 1 1 1 1 2 40 20 30 30 90.0 番号",
            ),
            "text": "ID 番号",
            "lines": [("ID 番号", 0.9, box(10.0, 20.0, 70.0, 50.0))],
        },
        {
            "name": "header only",
            "tsv": tsv(),
            "text": "",
            "lines": [],
        },
        {
            "name": "completely empty output",
            "tsv": "",
            "text": "",
            "lines": [],
        },
    ]

    def test_tsv_is_folded_into_lines(self) -> None:
        for case in self.CASES:
            with self.subTest(case["name"]):
                result = _parse_tsv(case["tsv"])
                self.assertEqual(result.text, case["text"])
                self.assertEqual(shape(result), case["lines"])

    def test_a_captured_page_parses_into_the_expected_lines(self) -> None:
        """One real capture, structural rows and all, as an anchor for the synthetic rows."""
        captured = (FIXTURES / "tesseract_page.tsv").read_text(encoding="utf-8")
        result = _parse_tsv(captured)

        self.assertEqual(
            result.text, "Hello dita OCR 2026\nThe quick brown fox\nsecond line"
        )
        self.assertEqual([line.text for line in result.lines], result.text.split("\n"))
        # The -1 word in the middle of line two is dropped from both text and confidence.
        self.assertNotIn("  ", result.lines[1].text)
        # (98.90 + 99.21 + 97.56 + 99.10) / 4 / 100, with the -1 word excluded.
        self.assertAlmostEqual(result.lines[1].confidence, 0.98692, places=5)
        self.assertEqual(result.lines[0].box, box(37.0, 41.0, 510.0, 91.0))


class ScriptedDecoder:
    """A stand-in for the decoder session: emits a chosen next token each step.

    Records the input_ids it was handed, because the loop re-feeding the whole growing
    prefix is our code and the exported graph has no cache to do it for us.
    """

    def __init__(self, token_ids: list, vocab_size: int = 16) -> None:
        self.script = list(token_ids)
        self.vocab_size = vocab_size
        self.seen_input_ids: list = []

    def run(self, _outputs, inputs):
        input_ids = inputs["input_ids"]
        self.seen_input_ids.append(input_ids[0].tolist())

        step = len(self.seen_input_ids) - 1
        chosen = self.script[step] if step < len(self.script) else 0
        logits = np.full((1, input_ids.shape[1], self.vocab_size), -10.0, dtype=np.float32)
        logits[0, -1, chosen] = 10.0
        return [logits]


class StubEncoder:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, _outputs, inputs):
        self.calls += 1
        self.pixel_values = inputs["pixel_values"]
        return [np.zeros((1, 197, 8), dtype=np.float32)]


# index:           0      1      2      3       4    5    6    7    8    9     10
MANGA_VOCAB = ["[PAD]", "[UNK]", "[CLS]", "[SEP]", "今", "日", "は", "い", "天", "気", "…"]


def manga_engine(script: list, max_tokens: int = 32) -> tuple:
    """A MangaOcrEngine whose sessions are fakes. __init__ is bypassed on purpose: it
    opens two ONNX graphs, and the decode loop is what this service actually owns."""
    engine = object.__new__(MangaOcrEngine)
    engine._encoder = StubEncoder()
    # The logit width is wider than the vocabulary on purpose, so a case can emit an id
    # the vocabulary does not cover.
    engine._decoder = ScriptedDecoder(script, vocab_size=len(MANGA_VOCAB) + 8)
    engine._vocab = list(MANGA_VOCAB)
    engine._image_size = (8, 8)
    engine._mean = np.asarray([0.5, 0.5, 0.5], dtype=np.float32)
    engine._std = np.asarray([0.5, 0.5, 0.5], dtype=np.float32)
    engine._rescale = 1 / 255
    engine._start_token = 2  # [CLS]
    engine._eos_token = 3  # [SEP]
    engine._max_tokens = max_tokens
    return engine, engine._decoder


def tiny_png() -> bytes:
    image = Image.new("RGB", (12, 20), "white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class MangaOcrDecodeTest(unittest.TestCase):
    """The greedy decode loop, which is ours: manga-ocr ships a bare graph."""

    CASES = [
        {
            "name": "tokens then EOS",
            "script": [4, 5, 6, 3],
            "max_tokens": 32,
            "text": "今日は",
            "decoder_calls": 4,
        },
        {
            "name": "EOS immediately gives an empty result",
            "script": [3],
            "max_tokens": 32,
            "text": "",
            "decoder_calls": 1,
        },
        {
            "name": "no EOS stops at max_tokens",
            "script": [4, 5, 6, 7],
            "max_tokens": 3,
            "text": "今日は",
            "decoder_calls": 3,
        },
        {
            "name": "special tokens in the stream are dropped from the text",
            "script": [4, 0, 5, 1, 3],
            "max_tokens": 32,
            "text": "今日",
            "decoder_calls": 5,
        },
        {
            "name": "an ellipsis is normalised to three dots",
            "script": [4, 10, 3],
            "max_tokens": 32,
            "text": "今...",
            "decoder_calls": 3,
        },
    ]

    def test_the_loop_assembles_one_line(self) -> None:
        for case in self.CASES:
            with self.subTest(case["name"]):
                engine, decoder = manga_engine(case["script"], case["max_tokens"])
                result = engine.infer(tiny_png())

                self.assertEqual(result.text, case["text"])
                self.assertEqual(len(decoder.seen_input_ids), case["decoder_calls"])
                if case["text"]:
                    self.assertEqual(len(result.lines), 1)
                    self.assertEqual(result.lines[0].text, case["text"])
                    self.assertIsNone(result.lines[0].box)
                else:
                    self.assertEqual(result.lines, [])

    def test_each_step_re_feeds_the_whole_prefix(self) -> None:
        """There is no key/value cache in the exported graph, so the loop must do this."""
        engine, decoder = manga_engine([4, 5, 3])
        engine.infer(tiny_png())

        self.assertEqual(decoder.seen_input_ids, [[2], [2, 4], [2, 4, 5]])

    def test_the_encoder_runs_once_with_a_normalised_nchw_batch(self) -> None:
        engine, _decoder = manga_engine([3])
        engine.infer(tiny_png())

        self.assertEqual(engine._encoder.calls, 1)
        pixel_values = engine._encoder.pixel_values
        self.assertEqual(pixel_values.shape, (1, 3, 8, 8))
        self.assertEqual(pixel_values.dtype, np.float32)
        # White at rescale 1/255 then (x - 0.5) / 0.5 lands on +1.0.
        self.assertAlmostEqual(float(pixel_values.max()), 1.0, places=5)
        # Greyscale first means all three channels are identical.
        self.assertTrue(np.array_equal(pixel_values[0, 0], pixel_values[0, 2]))

    def test_confidence_is_the_mean_of_the_per_token_softmax_maxima(self) -> None:
        engine, _decoder = manga_engine([4, 5, 3])
        result = engine.infer(tiny_png())

        # Every scripted step has the same logit gap, so every step scores the same.
        self.assertEqual(len(result.lines), 1)
        self.assertGreater(result.lines[0].confidence, 0.99)
        self.assertLessEqual(result.lines[0].confidence, 1.0)
        self.assertEqual(result.lines[0].confidence, round(result.lines[0].confidence, 5))

    def test_a_token_id_past_the_vocabulary_is_dropped(self) -> None:
        engine, _decoder = manga_engine([4, 15, 5, 3])
        self.assertEqual(engine.infer(tiny_png()).text, "今日")


class FakeRapidOcrOutput:
    def __init__(self, txts, scores, boxes) -> None:
        self.txts = txts
        self.scores = scores
        self.boxes = boxes


def rapidocr_engine(output) -> RapidOcrEngine:
    """A RapidOcrEngine wrapping a canned library result. __init__ is bypassed: it opens
    two ONNX sessions, and the assembly of lines is what this service owns."""
    engine = object.__new__(RapidOcrEngine)
    engine._ocr = lambda _array: output
    return engine


class RapidOcrResultTest(unittest.TestCase):
    """RapidOCR's parallel tuples into our line shape. The library owns everything else."""

    CASES = [
        {
            "name": "one line keeps its text, rounds the score and the box",
            "output": FakeRapidOcrOutput(
                txts=("Hello dita",),
                scores=(0.968123456,),
                boxes=np.asarray([[[37.04, 41.02], [509.0, 40.0], [510.0, 90.0], [37.0, 91.0]]]),
            ),
            "text": "Hello dita",
            "lines": [("Hello dita", 0.96812, [[37.0, 41.0], [509.0, 40.0], [510.0, 90.0], [37.0, 91.0]])],
        },
        {
            "name": "several lines keep their order and join with newlines",
            "output": FakeRapidOcrOutput(
                txts=("first", "second", "日本語"),
                scores=(0.5, 0.75, 0.999995),
                boxes=np.asarray(
                    [
                        [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]],
                        [[0.0, 2.0], [1.0, 2.0], [1.0, 3.0], [0.0, 3.0]],
                        [[0.0, 4.0], [1.0, 4.0], [1.0, 5.0], [0.0, 5.0]],
                    ]
                ),
            ),
            "text": "first\nsecond\n日本語",
            "lines": [
                ("first", 0.5, [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]),
                ("second", 0.75, [[0.0, 2.0], [1.0, 2.0], [1.0, 3.0], [0.0, 3.0]]),
                # 0.999995 rounds down at 5 places, it does not become 1.0.
                ("日本語", 0.99999, [[0.0, 4.0], [1.0, 4.0], [1.0, 5.0], [0.0, 5.0]]),
            ],
        },
        {
            "name": "no boxes leaves the box null rather than guessing",
            "output": FakeRapidOcrOutput(txts=("text",), scores=(0.9,), boxes=None),
            "text": "text",
            "lines": [("text", 0.9, None)],
        },
        {
            "name": "no scores leaves the confidence null",
            "output": FakeRapidOcrOutput(
                txts=("text",),
                scores=None,
                boxes=np.asarray([[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]]),
            ),
            "text": "text",
            "lines": [("text", None, [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]])],
        },
        {
            "name": "an empty result is empty, not a blank line",
            "output": FakeRapidOcrOutput(txts=(), scores=(), boxes=None),
            "text": "",
            "lines": [],
        },
        {
            "name": "the library returning None is an empty result",
            "output": None,
            "text": "",
            "lines": [],
        },
    ]

    def test_library_output_becomes_our_line_shape(self) -> None:
        image = tiny_png()
        for case in self.CASES:
            with self.subTest(case["name"]):
                result = rapidocr_engine(case["output"]).infer(image)
                self.assertEqual(result.text, case["text"])
                self.assertEqual(shape(result), case["lines"])

    def test_the_image_is_handed_over_as_bgr(self) -> None:
        """RapidOCR expects BGR; PIL decodes RGB. Getting this backwards is silent."""
        red = Image.new("RGB", (4, 4), (255, 0, 0))
        buffer = io.BytesIO()
        red.save(buffer, format="PNG")

        seen = {}
        engine = object.__new__(RapidOcrEngine)

        def capture(array):
            seen["array"] = array
            return FakeRapidOcrOutput(txts=(), scores=(), boxes=None)

        engine._ocr = capture
        engine.infer(buffer.getvalue())

        # Pure red in BGR is (0, 0, 255): the blue channel first, not the red one.
        self.assertEqual(tuple(seen["array"][0, 0]), (0, 0, 255))


class EngineFactoryTest(unittest.TestCase):
    """`build_engine` is the whole of the id -> adapter mapping."""

    CASES = [
        ("rapidocr", "ocr_worker.engines.rapidocr_engine.RapidOcrEngine"),
        ("tesseract", "ocr_worker.engines.tesseract_engine.TesseractEngine"),
        ("manga_ocr", "ocr_worker.engines.manga_ocr_engine.MangaOcrEngine"),
    ]

    def test_each_name_builds_its_adapter(self) -> None:
        model_dir = Path("/models/whatever")
        options = {"some": "option"}

        for name, target in self.CASES:
            with self.subTest(name):
                with mock.patch(target) as adapter:
                    built = build_engine(name, model_dir, options)
                self.assertIs(built, adapter.return_value)
                adapter.assert_called_once_with(model_dir, options)

    def test_every_advertised_engine_name_is_buildable(self) -> None:
        """ENGINE_NAMES is advertised in the handshake, so it must not drift."""
        self.assertEqual(sorted(ENGINE_NAMES), sorted(name for name, _ in self.CASES))

    def test_an_unknown_engine_names_the_ones_that_exist(self) -> None:
        with self.assertRaises(UnknownEngine) as caught:
            build_engine("ocropus", Path("/models"), {})
        message = str(caught.exception)
        self.assertIn("ocropus", message)
        for name in ENGINE_NAMES:
            self.assertIn(name, message)


class TesseractProcessTest(unittest.TestCase):
    """How we drive the binary: the argv, and every way the shell-out can go wrong.

    No tesseract on the PATH is required, and none is used. `subprocess.run` and
    `shutil.which` are the seam, because what this service owns is the command it builds
    and what it does with the result.
    """

    def engine(self, langs: str = "jpn+eng", options: Dict[str, Any] | None = None):
        """A TesseractEngine whose binary and language list are stubbed."""
        banner = "List of available languages in .../tessdata (3):\n"
        listing = _completed(0, (banner + langs.replace("+", "\n") + "\n").encode())
        with mock.patch.object(tesseract_engine.shutil, "which", return_value="/usr/bin/tesseract"), \
             mock.patch.object(tesseract_engine.subprocess, "run", return_value=listing):
            return TesseractEngine(Path("/models/tesseract"), options or {"lang": "jpn+eng"})

    def test_the_command_we_build(self) -> None:
        engine = self.engine()
        tsv_out = _completed(0, tsv("5 1 1 1 1 1 10 20 30 12 90.0 word").encode())

        with mock.patch.object(tesseract_engine.subprocess, "run", return_value=tsv_out) as run:
            result = engine.infer(b"png bytes")

        argv = run.call_args.args[0]
        self.assertEqual(
            argv,
            ["/usr/bin/tesseract", "stdin", "stdout", "-l", "jpn+eng", "--psm", "3", "tsv"],
        )
        self.assertEqual(run.call_args.kwargs["input"], b"png bytes")
        self.assertEqual(result.text, "word")

    def test_options_reach_the_command_line(self) -> None:
        engine = self.engine(langs="eng", options={"lang": "eng", "psm": 6})
        with mock.patch.object(
            tesseract_engine.subprocess, "run", return_value=_completed(0, tsv().encode())
        ) as run:
            engine.infer(b"png")
        argv = run.call_args.args[0]
        self.assertEqual(argv[argv.index("-l") + 1], "eng")
        self.assertEqual(argv[argv.index("--psm") + 1], "6")

    CONSTRUCTION_FAILURES = [
        {
            "name": "the binary is not on PATH",
            "which": None,
            "list_langs": _completed(0, b""),
            "fragment": "not on PATH",
        },
        {
            "name": "the requested language data is not installed",
            "which": "/usr/bin/tesseract",
            "list_langs": _completed(0, b"List of available languages (1):\neng\n"),
            "fragment": "missing language data for jpn",
        },
        {
            "name": "--list-langs itself fails",
            "which": "/usr/bin/tesseract",
            "list_langs": _completed(1, b"", b"TESSDATA_PREFIX is unset"),
            "fragment": "exited 1",
        },
    ]

    def test_construction_refuses_a_tesseract_it_cannot_use(self) -> None:
        for case in self.CONSTRUCTION_FAILURES:
            with self.subTest(case["name"]):
                with mock.patch.object(
                    tesseract_engine.shutil, "which", return_value=case["which"]
                ), mock.patch.object(
                    tesseract_engine.subprocess, "run", return_value=case["list_langs"]
                ):
                    with self.assertRaises(RuntimeError) as caught:
                        TesseractEngine(Path("/models/tesseract"), {"lang": "jpn+eng"})
                self.assertIn(case["fragment"], str(caught.exception))

    def test_a_non_zero_exit_from_the_ocr_run_is_raised_with_its_stderr(self) -> None:
        engine = self.engine()
        with mock.patch.object(
            tesseract_engine.subprocess,
            "run",
            return_value=_completed(2, b"", b"Error in pixReadStream: not a PNG"),
        ):
            with self.assertRaises(RuntimeError) as caught:
                engine.infer(b"not an image")
        self.assertIn("exited 2", str(caught.exception))
        self.assertIn("not a PNG", str(caught.exception))


class RecKeysTest(unittest.TestCase):
    """Materialising PP-OCRv5's CTC label set from the pinned inference.yml.

    The PaddlePaddle export carries no `character` metadata, so this file is the only
    thing standing between the recogniser and a vocabulary of nothing.
    """

    def write_yml(self, directory: Path, body: str) -> Path:
        path = directory / "inference.yml"
        path.write_text(body, encoding="utf-8")
        return path

    def test_the_keys_file_is_written_next_to_the_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # U+3000, the ideographic space, really is the first entry upstream. It needs
            # a double-quoted YAML scalar: single quotes do not process the escape.
            yml = self.write_yml(
                Path(tmp),
                'PostProcess:\n  character_dict:\n    - "\\u3000"\n    - 一\n    - A\n',
            )
            keys = _materialise_rec_keys(yml)

            self.assertEqual(keys, Path(tmp) / "rec_keys.txt")
            self.assertEqual(keys.read_text(encoding="utf-8").splitlines(), ["\u3000", "一", "A"])

    def test_an_existing_keys_file_is_reused_rather_than_rewritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            yml = self.write_yml(Path(tmp), "PostProcess:\n  character_dict:\n    - A\n")
            existing = Path(tmp) / "rec_keys.txt"
            existing.write_text("do not touch me\n", encoding="utf-8")

            self.assertEqual(_materialise_rec_keys(yml), existing)
            self.assertEqual(existing.read_text(encoding="utf-8"), "do not touch me\n")

    EMPTY_DICTS = [
        ("no PostProcess section", "Global:\n  model_name: x\n"),
        ("PostProcess with no character_dict", "PostProcess:\n  name: CTCLabelDecode\n"),
        ("an empty character_dict", "PostProcess:\n  character_dict: []\n"),
    ]

    def test_a_yml_without_a_label_set_is_refused(self) -> None:
        for name, body in self.EMPTY_DICTS:
            with self.subTest(name):
                with tempfile.TemporaryDirectory() as tmp:
                    yml = self.write_yml(Path(tmp), body)
                    with self.assertRaises(ValueError) as caught:
                        _materialise_rec_keys(yml)
                    self.assertIn("character_dict", str(caught.exception))


class EngineCloseTest(unittest.TestCase):
    """close() has to be safe to call twice: the manager calls it on every swap."""

    def test_each_adapter_releases_its_sessions(self) -> None:
        rapid = rapidocr_engine(FakeRapidOcrOutput(txts=(), scores=(), boxes=None))
        manga, _decoder = manga_engine([3])

        for name, engine, attributes in [
            ("rapidocr", rapid, ["_ocr"]),
            ("manga_ocr", manga, ["_encoder", "_decoder"]),
        ]:
            with self.subTest(name):
                engine.close()
                engine.close()  # idempotent: a failed swap can close twice
                for attribute in attributes:
                    self.assertIsNone(getattr(engine, attribute))



if __name__ == "__main__":
    unittest.main()
