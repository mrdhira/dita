"""Stdlib-only tests for the parts that must not regress: the protocol framing, the
registry parser, the fetcher's checksum refusal, and the one-model-resident invariant.

    python -m unittest discover -s tests -t .
"""

from __future__ import annotations

import io
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Dict
from unittest import mock

from ocr_worker import fetcher, protocol
from ocr_worker.engines import Engine, Line, Result
from ocr_worker.fetcher import ChecksumError, FetchError, ensure_model
from ocr_worker.manager import ModelManager, NoModelLoaded
from ocr_worker.registry import ModelFile, ModelSpec, RegistryError, load_registry
from ocr_worker.server import MAX_CONNECTIONS, Health, SocketServer, dispatch

REGISTRY_PATH = Path(__file__).resolve().parent.parent / "models.yaml"
GOOD_BYTES = b"pretend these are model weights"
GOOD_SHA = "68d8038c6e9a3441b0bcf0caebf52f563d112570cf95b50a869eae39c26bda46"


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
    def round_trip(self, control: Dict[str, Any], payload: bytes) -> None:
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

    def test_control_only_message(self) -> None:
        self.round_trip({"op": "list"}, b"")

    def test_payload_spanning_many_datagrams(self) -> None:
        payload = bytes(range(256)) * (protocol.MAX_CHUNK // 64)
        self.assertGreater(len(payload), protocol.MAX_CHUNK)
        self.round_trip({"op": "infer"}, payload)

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

    def test_a_model_id_must_be_a_safe_directory_name(self) -> None:
        """`id` becomes a directory under $MODELS_DIR, so traversal must be impossible."""
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "models.yaml"
            bad.write_text(
                "version: 1\n"
                "models:\n"
                "  - id: ../../escape\n"
                "    engine: tesseract\n"
                "    source:\n"
                "      type: system\n"
                "    files: []\n",
                encoding="utf-8",
            )
            with self.assertRaises(RegistryError) as caught:
                load_registry(bad)
            self.assertIn("safe directory name", str(caught.exception))

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

    def test_a_download_whose_digest_differs_is_refused(self) -> None:
        spec = self.spec_for(GOOD_SHA)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            fetcher.urllib.request, "urlopen", self.serve(b"tampered weights")
        ):
            with self.assertRaises(ChecksumError):
                ensure_model(Path(tmp), spec)
            self.assertEqual(list((Path(tmp) / "fixture").glob("*")), [])

    def test_a_corrupt_cached_file_is_never_trusted(self) -> None:
        spec = self.spec_for(GOOD_SHA)
        with tempfile.TemporaryDirectory() as tmp:
            planted = Path(tmp) / "fixture" / "weights.onnx"
            planted.parent.mkdir(parents=True)
            planted.write_bytes(b"stale rubbish")

            with mock.patch.object(
                fetcher.urllib.request, "urlopen", self.serve(b"still rubbish")
            ), self.assertRaises(ChecksumError):
                ensure_model(Path(tmp), spec)

    def test_a_verified_file_is_not_re_hashed_on_the_next_load(self) -> None:
        """Re-hashing 460 MB on every load is the thing being avoided here."""
        spec = self.spec_for(GOOD_SHA)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            fetcher.urllib.request, "urlopen", self.serve(GOOD_BYTES)
        ):
            ensure_model(Path(tmp), spec)
            with mock.patch.object(fetcher, "_sha256", side_effect=AssertionError("re-hashed")):
                ensure_model(Path(tmp), spec)

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

    def test_a_download_larger_than_the_pinned_size_is_cut_off(self) -> None:
        spec = self.spec_for(GOOD_SHA)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            fetcher.urllib.request, "urlopen", self.serve(GOOD_BYTES * 500)
        ):
            with self.assertRaises(FetchError) as caught:
                ensure_model(Path(tmp), spec)
            self.assertIn("more than", str(caught.exception))
            self.assertEqual(list((Path(tmp) / "fixture").glob("*")), [])

    def test_a_file_with_no_digest_is_refused_outright(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ChecksumError):
                ensure_model(Path(tmp), self.spec_for(None))


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

    def test_load_without_an_id_is_a_bad_request(self) -> None:
        response = dispatch(self.manager, {"op": "load"}, b"")
        self.assertEqual(response["error"]["code"], "bad_request")

    def test_unknown_op_is_named_in_the_error(self) -> None:
        response = dispatch(self.manager, {"op": "teleport"}, b"")
        self.assertEqual(response["error"]["code"], "bad_request")
        self.assertIn("teleport", response["error"]["message"])


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

    def test_a_freshly_bound_worker_passes_all_three(self) -> None:
        self.health.mark_bound()
        for op in ("livez", "readyz", "startupz"):
            response = dispatch(self.manager, {"op": op}, b"", self.health)
            self.assertTrue(response["ok"], f"{op}: {response.get('reasons')}")
            self.assertEqual(response["probe"], op)
            self.assertEqual(response["status"], "pass")

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
        for _ in range(100):
            if self.path.exists():
                break
            time.sleep(0.05)
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.server.stop)

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

    def test_connections_past_the_cap_are_told_they_are_refused(self) -> None:
        held = [self.connect() for _ in range(MAX_CONNECTIONS)]
        for sock in held:
            protocol.send_message(sock, {"op": "handshake"})
            self.assertTrue(protocol.recv_message(sock)[0]["ok"])

        extra = self.connect()
        response, _payload = protocol.recv_message(extra)
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "busy")


if __name__ == "__main__":
    unittest.main()
