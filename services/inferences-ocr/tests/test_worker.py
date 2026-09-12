"""Stdlib-only tests for the parts that must not regress: the protocol framing, the
registry parser, the fetcher's checksum refusal, and the one-model-resident invariant.

    python -m unittest discover -s tests -t .
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Dict
from unittest import mock

import numpy as np
from PIL import Image

from ocr_worker import fetcher, protocol
from ocr_worker.__main__ import probe_line, run_probe
from ocr_worker.engines import ENGINE_NAMES, Engine, Line, Result, UnknownEngine, build_engine
from ocr_worker.engines.manga_ocr_engine import MangaOcrEngine
from ocr_worker.engines.rapidocr_engine import RapidOcrEngine, _materialise_rec_keys
from ocr_worker.engines import tesseract_engine
from ocr_worker.engines.tesseract_engine import TesseractEngine, _parse_tsv
from ocr_worker.fetcher import ChecksumError, FetchError, ensure_model
from ocr_worker.manager import ModelManager, NoModelLoaded
from ocr_worker.registry import ModelFile, ModelSpec, RegistryError, load_registry
from ocr_worker.server import MAX_CONNECTIONS, Health, SocketServer, dispatch

REGISTRY_PATH = Path(__file__).resolve().parent.parent / "models.yaml"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
GOOD_BYTES = b"pretend these are model weights"
GOOD_SHA = "68d8038c6e9a3441b0bcf0caebf52f563d112570cf95b50a869eae39c26bda46"


def wait_until_listening(path: Path, timeout: float = 5.0) -> None:
    """Block until the server at `path` accepts a connection.

    Waiting for the socket *file* is not enough: it appears at bind(), which is before
    listen(), so a connect in that window fails with ECONNREFUSED. Probing with a real
    connection is the only wait that means what the caller wants.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as probe:
                probe.settimeout(timeout)
                probe.connect(str(path))
                return
        except OSError:
            time.sleep(0.02)
    raise AssertionError(f"no server listening at {path} after {timeout}s")


# ---------------------------------------------------------------------------
# On shape. The case-expansion tests below are subTest tables: one row per case,
# so a failure names the row and a new case is one line.
#
# The scenario tests are deliberately NOT tables, and should stay that way. A
# stalled peer, the connection cap, a cold load in flight, concurrent loads and
# a failed fetch each need their own threads, fixtures and teardown, and each
# fails in its own way. Folding them into a table would mean a row of optional
# setup flags and a loop full of branches -- the loop would become the thing
# under test. If you are here to "finish the job" and table-ify them: don't.
# ---------------------------------------------------------------------------


class FakeEngine(Engine):
    """Records its own lifecycle so a test can see whether it is still alive.

    Every construction is kept, keyed by instance rather than by engine name: keying by
    name would collapse repeated loads of the same engine and hide how many objects a
    leak actually stranded.
    """

    built: list = []
    _registry_lock = threading.Lock()

    def __init__(self, name: str) -> None:
        super().__init__(Path("."), {})
        self.name = name
        self.closed = False
        with FakeEngine._registry_lock:
            FakeEngine.built.append(self)

    @classmethod
    def alive(cls) -> list:
        with cls._registry_lock:
            return [engine for engine in cls.built if not engine.closed]

    @classmethod
    def last(cls, name: str) -> "FakeEngine":
        with cls._registry_lock:
            return [engine for engine in cls.built if engine.name == name][-1]

    def infer(self, image_bytes: bytes) -> Result:
        return Result(text=self.name, lines=[Line(text=self.name, confidence=1.0, box=None)])

    def close(self) -> None:
        self.closed = True


class ProtocolTest(unittest.TestCase):
    def round_trip(self, control: Dict[str, Any], payload: bytes) -> tuple:
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        received: list = []
        reader = threading.Thread(target=lambda: received.append(protocol.recv_message(right)))
        reader.start()
        protocol.send_message(left, control, payload)
        reader.join(timeout=10)
        left.close()
        right.close()

        self.assertEqual(received[0][0], control)
        self.assertEqual(received[0][1], payload)
        return received[0]

    def test_control_only_message(self) -> None:
        # round_trip asserts equality itself, but returning the decoded message lets the
        # expectation be visible here rather than only inside the helper.
        decoded_control, decoded_payload = self.round_trip({"op": "list"}, b"")
        self.assertEqual(decoded_control, {"op": "list"})
        self.assertEqual(decoded_payload, b"")

    def test_payload_spanning_many_datagrams(self) -> None:
        payload = bytes(range(256)) * (protocol.MAX_CHUNK // 64)
        self.assertGreater(len(payload), protocol.MAX_CHUNK)

        decoded_control, decoded_payload = self.round_trip({"op": "infer"}, payload)
        self.assertEqual(decoded_control, {"op": "infer"})
        self.assertEqual(decoded_payload, payload)
        self.assertEqual(len(decoded_payload), len(payload))

    def test_a_prologue_that_is_not_utf8_json_is_rejected(self) -> None:
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        left.send(b"\xff not json")
        with self.assertRaises(protocol.ProtocolError):
            protocol.recv_message(right)
        left.close()
        right.close()


    def test_a_control_block_far_larger_than_so_sndbuf_survives(self) -> None:
        """The regression: an infer response for a dense page used to die with EMSGSIZE.

        A single AF_UNIX datagram cannot exceed SO_SNDBUF, so the control block has to be
        chunked exactly like the payload. ~4000 OCR lines is well past that ceiling.
        """
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.addCleanup(left.close)
        self.addCleanup(right.close)

        line = {
            "text": "日本語のテキスト認識",
            "confidence": 0.99961,
            "box": [[39.0, 193.0], [507.0, 193.0], [507.0, 254.0], [39.0, 254.0]],
        }
        response = {"ok": True, "text": "x", "lines": [line] * 4000, "model": "rapidocr-ppocrv5"}
        encoded = json.dumps(response, ensure_ascii=False).encode("utf-8")
        send_buffer = left.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF)
        self.assertGreater(len(encoded), send_buffer, "fixture must exceed SO_SNDBUF to be a test")

        received: list = []
        reader = threading.Thread(target=lambda: received.append(protocol.recv_message(right)))
        reader.start()
        protocol.send_message(left, response)
        reader.join(timeout=10)

        self.assertEqual(received[0][0], response)

    def test_a_control_block_over_the_ceiling_is_refused_not_dropped(self) -> None:
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.addCleanup(left.close)
        self.addCleanup(right.close)

        oversized = {"ok": True, "text": "x" * (protocol.MAX_CONTROL + 1)}
        with self.assertRaises(protocol.ProtocolError):
            protocol.send_message(left, oversized)

    # Not a table row: this one owns a socket pair and a clock.
    def test_a_message_that_sends_fewer_datagrams_than_announced_times_out(self) -> None:
        """A peer that announces a payload and then stalls must not pin the reader."""
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.addCleanup(left.close)
        self.addCleanup(right.close)

        control = json.dumps({"op": "infer"}).encode("utf-8")
        left.send(
            json.dumps(
                {
                    "protocol": protocol.PROTOCOL_VERSION,
                    "control_len": len(control),
                    "payload_len": 4 * protocol.MAX_CHUNK,
                }
            ).encode("utf-8")
        )
        left.send(control)
        left.send(b"z" * protocol.MAX_CHUNK)  # one of the four announced chunks, then silence

        started = time.monotonic()
        with self.assertRaises(protocol.Timeout):
            protocol.recv_message(right, idle_timeout=5.0, message_timeout=0.25)
        self.assertLess(time.monotonic() - started, 5.0)

    def test_a_peer_that_closes_mid_message_is_reported_as_gone(self) -> None:
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.addCleanup(right.close)

        control = json.dumps({"op": "infer"}).encode("utf-8")
        left.send(
            json.dumps(
                {
                    "protocol": protocol.PROTOCOL_VERSION,
                    "control_len": len(control),
                    "payload_len": 99999,
                }
            ).encode("utf-8")
        )
        left.send(control)
        left.close()

        with self.assertRaises(protocol.PeerGone):
            protocol.recv_message(right, idle_timeout=5.0, message_timeout=5.0)

    def test_an_announced_length_over_the_ceiling_is_rejected(self) -> None:
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.addCleanup(left.close)
        self.addCleanup(right.close)

        left.send(
            json.dumps({"control_len": 10, "payload_len": protocol.MAX_PAYLOAD + 1}).encode("utf-8")
        )
        with self.assertRaises(protocol.ProtocolError):
            protocol.recv_message(right, idle_timeout=5.0, message_timeout=5.0)

    def test_an_oversized_datagram_is_caught_rather_than_truncated(self) -> None:
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.addCleanup(left.close)
        self.addCleanup(right.close)

        left.send(b"{" + b" " * (protocol.RECV_BUFFER * 2))
        with self.assertRaises(protocol.ProtocolError) as caught:
            protocol.recv_message(right, idle_timeout=5.0)
        self.assertIn("chunk limit", str(caught.exception))

    def test_a_json_array_control_block_is_rejected(self) -> None:
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.addCleanup(left.close)
        self.addCleanup(right.close)

        body = b"[1, 2, 3]"
        left.send(
            json.dumps(
                {"protocol": protocol.PROTOCOL_VERSION, "control_len": len(body), "payload_len": 0}
            ).encode("utf-8")
        )
        left.send(body)
        with self.assertRaises(protocol.ProtocolError) as caught:
            protocol.recv_message(right, idle_timeout=5.0)
        self.assertIn("must be a JSON object", str(caught.exception))


class RegistryTest(unittest.TestCase):
    def test_shipped_registry_parses(self) -> None:
        registry = load_registry(REGISTRY_PATH)
        self.assertEqual(registry.default_model, "rapidocr-ppocrv5")
        self.assertEqual(set(registry.models), {"rapidocr-ppocrv5", "tesseract", "manga-ocr"})

    def test_every_downloadable_file_has_a_pinned_digest(self) -> None:
        for spec in load_registry(REGISTRY_PATH).models.values():
            for spec_file in spec.files:
                self.assertTrue(spec_file.verified, f"{spec.id}/{spec_file.dest} has no sha256")
                self.assertEqual(len(spec_file.sha256), 64)

    MALFORMED = [
        (
            "an id that escapes the models directory",
            "models:\n  - id: ../../escape\n    engine: tesseract\n"
            "    source: {type: system}\n    files: []\n",
            "safe directory name",
        ),
        (
            "an id with a path separator",
            "models:\n  - id: a/b\n    engine: tesseract\n"
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
            "models:\n  - id: fine\n    engine: tesseract\n"
            "    source: {type: carrier-pigeon}\n    files: []\n",
            "expected huggingface or system",
        ),
        (
            "a huggingface source with no files",
            "models:\n  - id: fine\n    engine: rapidocr\n"
            "    source: {type: huggingface, repo: a/b, revision: abc}\n    files: []\n",
            "lists no files",
        ),
        (
            "a file with no pinned revision",
            "models:\n  - id: fine\n    engine: rapidocr\n"
            "    source: {type: huggingface, repo: a/b}\n"
            "    files:\n      - path: w.onnx\n",
            "immutable revision",
        ),
        (
            "a dest that climbs out of the model directory",
            "models:\n  - id: fine\n    engine: rapidocr\n"
            "    source: {type: huggingface, repo: a/b, revision: abc}\n"
            "    files:\n      - {path: w.onnx, dest: ../../w.onnx}\n",
            "must stay inside",
        ),
        (
            "two models sharing an id",
            "models:\n  - id: twin\n    engine: tesseract\n"
            "    source: {type: system}\n    files: []\n"
            "  - id: twin\n    engine: tesseract\n"
            "    source: {type: system}\n    files: []\n",
            "duplicate model id",
        ),
        (
            "a default_model that is not in the list",
            "default_model: ghost\nmodels:\n  - id: real\n    engine: tesseract\n"
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
        """Every one of these becomes a directory name or a download URL, so the parser
        is the only place that can stop them."""
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
        with self.assertRaises(RegistryError) as caught:
            load_registry(REGISTRY_PATH).get("nope")
        self.assertIn("unknown model 'nope'", str(caught.exception))


class FetcherTest(unittest.TestCase):
    """No network: urlopen is replaced so the checksum path is what is under test."""

    def spec_for(self, sha256: str | None) -> ModelSpec:
        return ModelSpec(
            id="fixture",
            description="",
            engine="rapidocr",
            langs=["en"],
            source_type="huggingface",
            files=[
                ModelFile(
                    repo="fixture/repo",
                    revision="0" * 40,
                    path="weights.onnx",
                    dest="weights.onnx",
                    sha256=sha256,
                    bytes=len(GOOD_BYTES),
                )
            ],
        )

    def serve(self, body: bytes):
        return lambda url, timeout=None: io.BytesIO(body)

    # Every way the fetched bytes can fail to be the pinned bytes. All of them must end
    # with nothing usable on disk, because a half-trusted model is worse than none.
    REFUSALS = [
        {
            # Same length as GOOD_BYTES on purpose: the size check runs first, and this
            # row is here to exercise the digest branch behind it.
            "name": "the download is the right size but the wrong bytes",
            "sha256": GOOD_SHA,
            "planted": None,
            "served": b"tampered bytes, same length!!!!",
            "error": ChecksumError,
            "fragment": "refusing to use it",
        },
        {
            "name": "a cached file does not match and neither does the re-download",
            "sha256": GOOD_SHA,
            "planted": b"stale rubbish, same length!!!!!",
            "served": b"PRETEND THESE ARE MODEL WEIGHTS",
            "error": ChecksumError,
            "fragment": "refusing to use it",
        },
        {
            "name": "the download is shorter than the pinned size",
            "sha256": GOOD_SHA,
            "planted": None,
            "served": b"too short",
            "error": ChecksumError,
            "fragment": "models.yaml pins 31",
        },
        {
            "name": "the registry pins no digest at all",
            "sha256": None,
            "planted": None,
            "served": GOOD_BYTES,
            "error": ChecksumError,
            "fragment": "no pinned sha256",
        },
        {
            "name": "the download runs past the pinned size",
            "sha256": GOOD_SHA,
            "planted": None,
            "served": GOOD_BYTES * 500,
            "error": FetchError,
            "fragment": "more than",
        },
    ]

    def test_bytes_that_are_not_the_pinned_bytes_are_refused(self) -> None:
        for case in self.REFUSALS:
            with self.subTest(case["name"]):
                spec = self.spec_for(case["sha256"])
                with tempfile.TemporaryDirectory() as tmp:
                    if case["planted"] is not None:
                        planted = Path(tmp) / "fixture" / "weights.onnx"
                        planted.parent.mkdir(parents=True)
                        planted.write_bytes(case["planted"])

                    with mock.patch.object(
                        fetcher.urllib.request, "urlopen", self.serve(case["served"])
                    ):
                        with self.assertRaises(case["error"]) as caught:
                            ensure_model(Path(tmp), spec)

                    self.assertIn(case["fragment"], str(caught.exception))
                    self.assertEqual(
                        list((Path(tmp) / "fixture").glob("*")),
                        [],
                        "a refused attempt must leave nothing behind",
                    )

    def test_matching_digest_is_accepted_and_cached(self) -> None:
        spec = self.spec_for(GOOD_SHA)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            fetcher.urllib.request, "urlopen", self.serve(GOOD_BYTES)
        ):
            written = ensure_model(Path(tmp), spec)
            self.assertEqual(written[0].read_bytes(), GOOD_BYTES)

            # Second call must not download again; urlopen is removed to prove it.
            with mock.patch.object(fetcher.urllib.request, "urlopen", self.serve(b"wrong")):
                self.assertEqual(ensure_model(Path(tmp), spec), written)

    def test_a_verified_file_is_not_re_hashed_on_the_next_load(self) -> None:
        """Re-hashing 460 MB on every load is the thing being avoided here.

        The mechanism is a _sha256 that raises if called, so a regression fails loudly.
        The assertions below spell that out: it is called on the first load and not on
        the second, and the second still returns the file.
        """
        spec = self.spec_for(GOOD_SHA)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            fetcher.urllib.request, "urlopen", self.serve(GOOD_BYTES)
        ):
            with mock.patch.object(fetcher, "_sha256", wraps=fetcher._sha256) as hashed:
                first = ensure_model(Path(tmp), spec)
            self.assertGreater(hashed.call_count, 0, "the first load must verify the bytes")

            with mock.patch.object(
                fetcher, "_sha256", side_effect=AssertionError("re-hashed a verified file")
            ) as not_hashed:
                second = ensure_model(Path(tmp), spec)
            self.assertEqual(not_hashed.call_count, 0)
            self.assertEqual(second, first)
            self.assertEqual(second[0].read_bytes(), GOOD_BYTES)

    def test_a_file_rewritten_behind_our_back_is_hashed_again(self) -> None:
        spec = self.spec_for(GOOD_SHA)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            fetcher.urllib.request, "urlopen", self.serve(GOOD_BYTES)
        ):
            written = ensure_model(Path(tmp), spec)[0]
            written.write_bytes(b"swapped in after verification!!")  # same length, new bytes
            with mock.patch.object(
                fetcher.urllib.request, "urlopen", self.serve(b"still wrong")
            ), self.assertRaises(ChecksumError):
                ensure_model(Path(tmp), spec)

    def test_an_endpoint_with_a_hostile_scheme_is_refused(self) -> None:
        with mock.patch.dict(os.environ, {"HF_ENDPOINT": "file:///etc"}):
            with self.assertRaises(FetchError):
                fetcher.endpoint()
        with mock.patch.dict(os.environ, {"HF_ENDPOINT": "https://mirror.example"}):
            self.assertEqual(fetcher.endpoint(), "https://mirror.example")


class OneModelResidentTest(unittest.TestCase):
    """The invariant, checked on the objects rather than on process RSS."""

    def setUp(self) -> None:
        FakeEngine.built.clear()
        self.registry = load_registry(REGISTRY_PATH)
        self.manager = ModelManager(self.registry, Path("/nonexistent"))

        # Swap out fetching and engine construction; this test is about the manager.
        import ocr_worker.manager as manager_module

        self._real_ensure = manager_module.fetcher.ensure_model
        self._real_build = manager_module.build_engine
        manager_module.fetcher.ensure_model = lambda models_dir, spec: []
        manager_module.build_engine = lambda engine, model_dir, options: FakeEngine(engine)
        self._module = manager_module

    def tearDown(self) -> None:
        self._module.fetcher.ensure_model = self._real_ensure
        self._module.build_engine = self._real_build

    def test_loading_b_closes_a_and_leaves_exactly_one_alive(self) -> None:
        self.manager.load("rapidocr-ppocrv5")
        self.assertEqual(self.manager.resident()["id"], "rapidocr-ppocrv5")

        result = self.manager.load("manga-ocr")
        self.assertEqual(result["unloaded"], "rapidocr-ppocrv5")
        self.assertEqual(self.manager.resident()["id"], "manga-ocr")

        self.assertEqual([engine.name for engine in FakeEngine.alive()], ["manga_ocr"])
        self.assertTrue(FakeEngine.last("rapidocr").closed)

    def test_already_resident_reports_the_unloaded_field(self) -> None:
        self.manager.load("tesseract")
        again = self.manager.load("tesseract")
        self.assertTrue(again["already_resident"])
        self.assertIn("unloaded", again)
        self.assertIsNone(again["unloaded"])
        # No second engine was constructed for the same model.
        self.assertEqual(len(FakeEngine.built), 1)

    def test_infer_without_a_loaded_model_fails_clearly(self) -> None:
        with self.assertRaises(NoModelLoaded):
            self.manager.infer(b"image")

        response = dispatch(self.manager, {"op": "infer"}, b"image")
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "no_model_loaded")

    def test_unload_leaves_nothing_resident(self) -> None:
        self.manager.load("tesseract")
        self.assertEqual(self.manager.unload(), {"unloaded": "tesseract"})
        self.assertIsNone(self.manager.resident())
        self.assertTrue(FakeEngine.last("tesseract").closed)

    # Not a table row: 24 threads and a probe wedged into build_engine.
    def test_concurrent_loads_never_leave_two_engines_alive(self) -> None:
        """Sample from inside the critical section, not after the lock is released.

        `build_engine` runs while the manager holds its exclusive lock, so counting live
        engines there observes the swap itself rather than a settled global state.
        """
        observed: list = []

        def counting_build(engine: str, model_dir: Path, options: Dict[str, Any]) -> FakeEngine:
            observed.append(len(FakeEngine.alive()))
            return FakeEngine(engine)

        self._module.build_engine = counting_build

        ids = ["rapidocr-ppocrv5", "manga-ocr", "tesseract"] * 8
        threads = [threading.Thread(target=self.manager.load, args=(i,)) for i in ids]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertTrue(observed)
        # Zero live engines at every construction: the previous one is always released
        # first, so two models are never in memory at once.
        self.assertEqual(max(observed), 0)
        self.assertEqual(len(FakeEngine.alive()), 1)

    def test_a_failed_fetch_does_not_evict_the_resident_model(self) -> None:
        self.manager.load("rapidocr-ppocrv5")
        loaded = FakeEngine.last("rapidocr")

        def failing_fetch(models_dir: Path, spec: Any) -> list:
            raise FetchError("the network went away")

        self._module.fetcher.ensure_model = failing_fetch

        with self.assertRaises(FetchError):
            self.manager.load("manga-ocr")

        # Still serving the model it had before the failed attempt.
        self.assertEqual(self.manager.resident()["id"], "rapidocr-ppocrv5")
        self.assertFalse(loaded.closed)
        self.assertEqual(self.manager.infer(b"image")["model"], "rapidocr-ppocrv5")

    # Not a table row: two threads rendezvousing on events.
    def test_inference_is_not_blocked_while_a_model_downloads(self) -> None:
        """The exclusive lock must not be held across the fetch."""
        self.manager.load("rapidocr-ppocrv5")

        fetch_started = threading.Event()
        release_fetch = threading.Event()
        inferred_during_fetch = []

        def slow_fetch(models_dir: Path, spec: Any) -> list:
            fetch_started.set()
            release_fetch.wait(timeout=10)
            return []

        self._module.fetcher.ensure_model = slow_fetch

        loader = threading.Thread(target=self.manager.load, args=("manga-ocr",))
        loader.start()
        self.assertTrue(fetch_started.wait(timeout=10))

        # The old model answers while the new one is still downloading.
        inferred_during_fetch.append(self.manager.infer(b"image")["model"])
        self.assertEqual(self.manager.list()["loading"], "manga-ocr")

        release_fetch.set()
        loader.join(timeout=10)

        self.assertEqual(inferred_during_fetch, ["rapidocr-ppocrv5"])
        self.assertEqual(self.manager.resident()["id"], "manga-ocr")


class DispatchTest(unittest.TestCase):
    """`dispatch` is the whole request surface; the socket server is a thin wrapper."""

    def setUp(self) -> None:
        FakeEngine.built.clear()
        import ocr_worker.manager as manager_module

        self._module = manager_module
        self._real_ensure = manager_module.fetcher.ensure_model
        self._real_build = manager_module.build_engine
        manager_module.fetcher.ensure_model = lambda models_dir, spec: []
        manager_module.build_engine = lambda engine, model_dir, options: FakeEngine(engine)
        self.manager = ModelManager(load_registry(REGISTRY_PATH), Path("/nonexistent"))

    def tearDown(self) -> None:
        self._module.fetcher.ensure_model = self._real_ensure
        self._module.build_engine = self._real_build

    def test_handshake_advertises_the_limits_a_peer_needs(self) -> None:
        response = dispatch(self.manager, {"op": "handshake"}, b"")
        self.assertTrue(response["ok"])
        self.assertEqual(response["protocol"], protocol.PROTOCOL_VERSION)
        for field in ("max_chunk", "max_control", "max_payload"):
            self.assertIn(field, response["limits"])
        self.assertEqual(response["limits"]["max_chunk"], protocol.MAX_CHUNK)
        self.assertIsNone(response["resident"])

    def test_list_then_load_then_unload(self) -> None:
        listing = dispatch(self.manager, {"op": "list"}, b"")
        self.assertTrue(listing["ok"])
        self.assertEqual(listing["default_model"], "rapidocr-ppocrv5")
        self.assertIn("rapidocr-ppocrv5", [m["id"] for m in listing["models"]])

        loaded = dispatch(self.manager, {"op": "load", "id": "rapidocr-ppocrv5"}, b"")
        self.assertTrue(loaded["ok"])
        self.assertIsNone(loaded["unloaded"])

        self.assertEqual(
            dispatch(self.manager, {"op": "handshake"}, b"")["resident"]["id"], "rapidocr-ppocrv5"
        )
        self.assertEqual(dispatch(self.manager, {"op": "unload"}, b"")["unloaded"], "rapidocr-ppocrv5")

    BAD_REQUESTS = [
        ("load with no id", {"op": "load"}, b"", "bad_request", "`id`"),
        ("an op that does not exist", {"op": "teleport"}, b"", "bad_request", "teleport"),
        ("no op at all", {}, b"", "bad_request", "None"),
        ("an op of the wrong type", {"op": 7}, b"", "bad_request", "7"),
        # An empty payload is rejected before the resident-model check, so this is
        # bad_request rather than no_model_loaded.
        ("infer with an empty payload", {"op": "infer"}, b"", "bad_request", "image bytes"),
        ("infer with bytes but nothing loaded", {"op": "infer"}, b"png",
         "no_model_loaded", "load"),
        ("load of a model the registry does not have", {"op": "load", "id": "nope"},
         b"", "unknown_model", "nope"),
    ]

    def test_malformed_requests_get_a_coded_error(self) -> None:
        for name, control, payload, code, fragment in self.BAD_REQUESTS:
            with self.subTest(name):
                response = dispatch(self.manager, control, payload)
                self.assertFalse(response["ok"])
                self.assertEqual(response["error"]["code"], code)
                self.assertIn(fragment, response["error"]["message"])


class HealthTest(unittest.TestCase):
    """livez / readyz / startupz, and what each one is allowed to be false for."""

    def setUp(self) -> None:
        FakeEngine.built.clear()
        import ocr_worker.manager as manager_module

        self._module = manager_module
        self._real_ensure = manager_module.fetcher.ensure_model
        self._real_build = manager_module.build_engine
        manager_module.fetcher.ensure_model = lambda models_dir, spec: []
        manager_module.build_engine = lambda engine, model_dir, options: FakeEngine(engine)

        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.manager = ModelManager(load_registry(REGISTRY_PATH), Path(self.directory.name))
        self.health = Health(self.manager)

    def tearDown(self) -> None:
        self._module.fetcher.ensure_model = self._real_ensure
        self._module.build_engine = self._real_build

    PROBE_OPS = ("livez", "readyz", "startupz")

    def test_a_freshly_bound_worker_passes_all_three(self) -> None:
        self.health.mark_bound()
        for op in self.PROBE_OPS:
            with self.subTest(op):
                response = dispatch(self.manager, {"op": op}, b"", self.health)
                self.assertTrue(response["ok"], response.get("reasons"))
                self.assertEqual(response["probe"], op)
                self.assertEqual(response["status"], "pass")
                self.assertEqual(response["reasons"], [])
                self.assertIsInstance(response["uptime_s"], float)

    def test_shutdown_fails_liveness_but_startup_still_reports_a_finished_boot(self) -> None:
        """The probes answer different questions, so they must not move together."""
        self.health.mark_bound()
        self.health.mark_stopped()

        expected = {"livez": False, "readyz": False, "startupz": True}
        for op in self.PROBE_OPS:
            with self.subTest(op):
                response = dispatch(self.manager, {"op": op}, b"", self.health)
                self.assertEqual(response["ok"], expected[op], response.get("reasons"))

    def test_startupz_fails_until_the_socket_is_bound(self) -> None:
        before = dispatch(self.manager, {"op": "startupz"}, b"", self.health)
        self.assertFalse(before["ok"])
        self.assertIn("not bound", " ".join(before["reasons"]))

        self.health.mark_bound()
        self.assertTrue(dispatch(self.manager, {"op": "startupz"}, b"", self.health)["ok"])

    def test_livez_ignores_dependencies_and_only_fails_on_shutdown(self) -> None:
        self.health.mark_bound()
        self.manager._last_error = "load of anything failed: boom"
        self.assertTrue(dispatch(self.manager, {"op": "livez"}, b"", self.health)["ok"])

        self.health.mark_stopped()
        self.assertFalse(dispatch(self.manager, {"op": "livez"}, b"", self.health)["ok"])

    def test_readyz_stays_true_with_nothing_loaded_yet(self) -> None:
        """A worker that has simply never been given a model can still be given one."""
        self.health.mark_bound()
        response = dispatch(self.manager, {"op": "readyz"}, b"", self.health)
        self.assertTrue(response["ok"])
        self.assertIsNone(response["resident"])

    def test_readyz_goes_false_after_a_failed_load_with_nothing_resident(self) -> None:
        self.health.mark_bound()

        def failing_fetch(models_dir: Path, spec: Any) -> list:
            raise FetchError("the network went away")

        self._module.fetcher.ensure_model = failing_fetch
        with self.assertRaises(FetchError):
            self.manager.load("rapidocr-ppocrv5")

        response = dispatch(self.manager, {"op": "readyz"}, b"", self.health)
        self.assertFalse(response["ok"])
        self.assertIn("the network went away", " ".join(response["reasons"]))

        # A later successful load clears it: readiness is a current verdict, not a scar.
        self._module.fetcher.ensure_model = self._real_ensure
        self._module.fetcher.ensure_model = lambda models_dir, spec: []
        self.manager.load("rapidocr-ppocrv5")
        self.assertTrue(dispatch(self.manager, {"op": "readyz"}, b"", self.health)["ok"])

    def test_readyz_reports_an_unwritable_models_directory(self) -> None:
        self.health.mark_bound()
        unwritable = Path(self.directory.name) / "locked"
        unwritable.mkdir()
        unwritable.chmod(0o500)
        self.addCleanup(unwritable.chmod, 0o700)

        manager = ModelManager(load_registry(REGISTRY_PATH), unwritable)
        health = Health(manager)
        health.mark_bound()
        response = dispatch(manager, {"op": "readyz"}, b"", health)
        self.assertFalse(response["ok"])
        self.assertIn("not writable", " ".join(response["reasons"]))

    # Not a table row: a fetch held open while the probe runs.
    def test_a_cold_load_in_flight_does_not_trip_readiness(self) -> None:
        """The compose healthcheck must survive a first load that downloads 460 MB."""
        self.health.mark_bound()
        fetch_started = threading.Event()
        release = threading.Event()

        def slow_fetch(models_dir: Path, spec: Any) -> list:
            fetch_started.set()
            release.wait(timeout=10)
            return []

        self._module.fetcher.ensure_model = slow_fetch
        loader = threading.Thread(target=self.manager.load, args=("manga-ocr",))
        loader.start()
        self.assertTrue(fetch_started.wait(timeout=10))

        response = dispatch(self.manager, {"op": "readyz"}, b"", self.health)
        self.assertTrue(response["ok"])
        self.assertEqual(response["loading"], "manga-ocr")

        release.set()
        loader.join(timeout=10)


class SocketServerTest(unittest.TestCase):
    """The real server over a real unix socket, with a stub manager in place of engines."""

    LINES = 4000

    class StubManager:
        """Enough of ModelManager for dispatch, returning a deliberately huge result."""

        def __init__(self, registry, line_count: int, models_dir: Path) -> None:
            self.registry = registry
            self.loading = None
            self.models_dir = models_dir
            self.last_error = None
            self._line_count = line_count

        def list(self):
            return {"models": [], "default_model": self.registry.default_model, "resident": None}

        def resident(self):
            return None

        def infer(self, image_bytes: bytes):
            line = {
                "text": "日本語のテキスト認識",
                "confidence": 0.99961,
                "box": [[39.0, 193.0], [507.0, 193.0], [507.0, 254.0], [39.0, 254.0]],
            }
            return {"text": "x", "lines": [line] * self._line_count, "model": "stub", "infer_ms": 1.0}

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "ocr.sock"

        manager = self.StubManager(load_registry(REGISTRY_PATH), self.LINES, Path(self.directory.name))
        self.server = SocketServer(manager, self.path, idle_timeout=10.0, message_timeout=0.5)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        # Cleanups run last-registered-first: stop the accept loop, then join it.
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.server.stop)
        wait_until_listening(self.path)

    def connect(self) -> socket.socket:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        sock.settimeout(10)
        sock.connect(str(self.path))
        self.addCleanup(sock.close)
        return sock

    def test_a_response_far_over_so_sndbuf_arrives_intact(self) -> None:
        """End to end over a real socket: before the framing fix this closed the connection."""
        sock = self.connect()
        protocol.send_message(sock, {"op": "infer"}, b"pretend png bytes")
        response, _payload = protocol.recv_message(sock)

        size = len(json.dumps(response, ensure_ascii=False).encode("utf-8"))
        self.assertGreater(size, sock.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF))
        self.assertTrue(response["ok"])
        self.assertEqual(len(response["lines"]), self.LINES)

    def test_a_stalled_peer_is_timed_out_with_an_error_not_a_reset(self) -> None:
        sock = self.connect()
        control = json.dumps({"op": "infer"}).encode("utf-8")
        sock.send(
            json.dumps(
                {
                    "protocol": protocol.PROTOCOL_VERSION,
                    "control_len": len(control),
                    "payload_len": 4 * protocol.MAX_CHUNK,
                }
            ).encode("utf-8")
        )
        sock.send(control)
        sock.send(b"z" * protocol.MAX_CHUNK)  # one of four announced chunks, then nothing

        response, _payload = protocol.recv_message(sock)
        self.assertEqual(response["error"]["code"], "timeout")

    def test_the_probe_ops_answer_over_the_socket(self) -> None:
        """What the container healthcheck execs has to work on the real transport."""
        sock = self.connect()
        for op in ("livez", "readyz", "startupz"):
            protocol.send_message(sock, {"op": op})
            response, _payload = protocol.recv_message(sock)
            self.assertTrue(response["ok"], f"{op}: {response.get('reasons')}")
            self.assertEqual(response["probe"], op)

    # Not a table row: it holds 16 live sockets open at once.
    def test_connections_past_the_cap_are_told_they_are_refused(self) -> None:
        held = [self.connect() for _ in range(MAX_CONNECTIONS)]
        for sock in held:
            protocol.send_message(sock, {"op": "handshake"})
            self.assertTrue(protocol.recv_message(sock)[0]["ok"])

        extra = self.connect()
        response, _payload = protocol.recv_message(extra)
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "busy")


class ProbeLineTest(unittest.TestCase):
    """The exact string `--probe` prints.

    This exists because the verdict used to be printed twice -- `readyz: pass pass` -- and
    the exit code was correct the whole time, so nothing caught it. These assert the line,
    byte for byte.
    """

    CASES = [
        {
            "name": "a passing readyz names the resident model and the uptime",
            "op": "readyz",
            "response": {
                "ok": True,
                "probe": "readyz",
                "status": "pass",
                "uptime_s": 12.345,
                "reasons": [],
                "resident": {"id": "rapidocr-ppocrv5", "engine": "rapidocr"},
                "loading": None,
            },
            "line": "readyz: pass resident=rapidocr-ppocrv5 uptime=12.3s",
        },
        {
            "name": "a passing readyz with nothing loaded says so",
            "op": "readyz",
            "response": {
                "ok": True, "status": "pass", "uptime_s": 2.0, "reasons": [],
                "resident": None, "loading": None,
            },
            "line": "readyz: pass resident=none uptime=2.0s",
        },
        {
            "name": "a passing readyz during a load names the incoming model",
            "op": "readyz",
            "response": {
                "ok": True, "status": "pass", "uptime_s": 5.06, "reasons": [],
                "resident": None, "loading": "manga-ocr",
            },
            "line": "readyz: pass resident=none loading=manga-ocr uptime=5.1s",
        },
        {
            "name": "livez carries no resident field",
            "op": "livez",
            "response": {"ok": True, "status": "pass", "uptime_s": 41.24, "reasons": []},
            "line": "livez: pass uptime=41.2s",
        },
        {
            "name": "startupz carries no resident field",
            "op": "startupz",
            "response": {"ok": True, "status": "pass", "uptime_s": 41.24, "reasons": []},
            "line": "startupz: pass uptime=41.2s",
        },
        {
            "name": "a failing probe prints the reason",
            "op": "readyz",
            "response": {
                "ok": False, "status": "fail", "uptime_s": 0.5,
                "reasons": ["the models directory /models is not writable"],
                "resident": None, "loading": None,
            },
            "line": "readyz: fail the models directory /models is not writable",
        },
        {
            "name": "several reasons are joined",
            "op": "readyz",
            "response": {
                "ok": False, "status": "fail", "uptime_s": 0.5,
                "reasons": ["first thing", "second thing"],
            },
            "line": "readyz: fail first thing; second thing",
        },
        {
            "name": "a failure with no reasons still says something",
            "op": "livez",
            "response": {"ok": False, "status": "fail", "reasons": []},
            "line": "livez: fail no reason given",
        },
        {
            "name": "a missing uptime is omitted rather than printed as None",
            "op": "livez",
            "response": {"ok": True, "status": "pass", "reasons": []},
            "line": "livez: pass",
        },
    ]

    def test_the_printed_line(self) -> None:
        for case in self.CASES:
            with self.subTest(case["name"]):
                self.assertEqual(probe_line(case["op"], case["response"]), case["line"])

    def test_the_verdict_is_never_printed_twice(self) -> None:
        """The actual regression: `status` holds the same word as the verdict."""
        for case in self.CASES:
            with self.subTest(case["name"]):
                line = case["line"]
                verdict = "pass" if case["response"]["ok"] else "fail"
                self.assertNotIn("pass pass", line)
                self.assertNotIn("fail fail", line)
                self.assertEqual(line.split().count(verdict), 1, line)


class ProbeCLITest(unittest.TestCase):
    """`--probe` end to end: a real socket, a real server, the real printed line."""

    def start_worker(self, models_dir: Path) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "ocr.sock"

        manager = ModelManager(load_registry(REGISTRY_PATH), models_dir)
        server = SocketServer(manager, path, idle_timeout=10.0, message_timeout=5.0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        # Cleanups run last-registered-first: stop the accept loop, then join it.
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.stop)
        wait_until_listening(path)
        return path

    def probe(self, path: Path, name: str) -> tuple:
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            code = run_probe(path, name)
        return code, captured.getvalue().strip()

    def test_a_passing_probe_prints_one_verdict_and_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory() as models:
            path = self.start_worker(Path(models))

            code, line = self.probe(path, "ready")
            self.assertEqual(code, 0)
            self.assertNotIn("pass pass", line)
            self.assertRegex(line, r"^readyz: pass resident=none uptime=\d+\.\d+s$")

            code, line = self.probe(path, "live")
            self.assertEqual(code, 0)
            self.assertRegex(line, r"^livez: pass uptime=\d+\.\d+s$")

            code, line = self.probe(path, "startup")
            self.assertEqual(code, 0)
            self.assertRegex(line, r"^startupz: pass uptime=\d+\.\d+s$")

    def test_a_failing_probe_prints_the_reason_and_exits_one(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            models = Path(parent) / "readonly"
            models.mkdir()
            models.chmod(0o500)
            try:
                path = self.start_worker(models)
                code, line = self.probe(path, "ready")

                self.assertEqual(code, 1)
                self.assertEqual(line, f"readyz: fail the models directory {models} is not writable")

                # Liveness is deliberately indifferent to a broken dependency.
                code, line = self.probe(path, "live")
                self.assertEqual(code, 0)
                self.assertRegex(line, r"^livez: pass uptime=\d+\.\d+s$")
            finally:
                # Restore before the TemporaryDirectory tries to remove it.
                models.chmod(0o700)


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
