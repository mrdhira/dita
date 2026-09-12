"""Digest verification: what happens to bytes that are not the pinned bytes.

No network -- urlopen is replaced, so the checksum path is what is under test. The table is
every way a fetch can end up with the wrong file; all of them must leave nothing behind,
because a half-trusted model is worse than none.
"""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dita_worker import ChecksumError, FetchError, ModelFile, ModelSpec, ensure_model, fetcher

GOOD_BYTES = b"pretend these are model weights"
GOOD_SHA = "68d8038c6e9a3441b0bcf0caebf52f563d112570cf95b50a869eae39c26bda46"


class FetcherTest(unittest.TestCase):
    def spec_for(self, sha256: str | None) -> ModelSpec:
        return ModelSpec(
            id="fixture",
            description="",
            engine="echo",
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
