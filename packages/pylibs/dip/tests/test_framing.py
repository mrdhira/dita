"""The framing half of the conformance corpus: `specs/dip/conformance/framing.json`.

Cases are datagrams rather than sockets, so they drive the decoder through the same reader
interface `recv_message` binds to a file descriptor. Exhaustion is where the two incomplete
readers differ -- a peer that closed reads empty, a peer that stalled times out -- and the
corpus calls both of them `incomplete`, so every case is run both ways.
"""

from __future__ import annotations

import hashlib
import json
import socket
import unittest
from base64 import b64decode
from collections.abc import Callable
from typing import Any

from dip import (
    MAX_CHUNK,
    MAX_CONTROL,
    MAX_PAYLOAD,
    PROTOCOL_VERSION,
    RECV_BUFFER,
    DatagramReader,
    ErrorCode,
    PeerGone,
    ProtocolError,
    Timeout,
    encode_message,
    read_message,
    send_message,
)

from . import corpus

CORPUS = corpus.load("framing.json")

# `control_shape` is structural, used where echoing 220 KB of JSON would only make the
# corpus large. A key the corpus grows and this table does not know is a failure, not a
# silent pass.
SHAPE: dict[str, Callable[[dict[str, Any]], Any]] = {
    "control_len": lambda control: len(json.dumps(control).encode("utf-8")),
    "lines": lambda control: len(control["lines"]),
    "first_line_text": lambda control: control["lines"][0]["text"],
    "last_line_n": lambda control: control["lines"][-1]["n"],
}


def reader(datagrams: list[bytes], stalls: bool) -> DatagramReader:
    """A reader over a fixed list of datagrams, truncating the way the kernel does."""
    remaining = list(datagrams)

    def read(timeout: float | None) -> tuple[bytes, bool]:
        if not remaining:
            if stalls:
                raise Timeout(f"peer sent nothing for {timeout}s")
            return b"", False
        datagram = remaining.pop(0)
        return datagram[:RECV_BUFFER], len(datagram) > RECV_BUFFER

    return read


def verdict(datagrams: list[bytes], stalls: bool = False) -> dict[str, Any]:
    """What a receiver makes of these datagrams: the mapping `serve_connection` applies."""
    try:
        control, payload = read_message(reader(datagrams, stalls))
    except ProtocolError as exc:
        return {"outcome": "reject", "error": ErrorCode.bad_request, "detail": str(exc)}
    except (Timeout, PeerGone) as exc:
        return {"outcome": "incomplete", "detail": str(exc)}
    return {"outcome": "accept", "control": control, "payload": payload}


class FramingCorpusTest(unittest.TestCase):
    def test_the_corpus_pins_the_wire_this_package_speaks(self) -> None:
        self.assertEqual(CORPUS["corpus"], "dip-framing")
        self.assertEqual(CORPUS["protocol"], PROTOCOL_VERSION)
        self.assertEqual(
            CORPUS["limits"],
            {"max_chunk": MAX_CHUNK, "max_control": MAX_CONTROL, "max_payload": MAX_PAYLOAD},
        )

    def test_every_case(self) -> None:
        for case in CORPUS["cases"]:
            with self.subTest(case["name"]):
                datagrams = [b64decode(datagram) for datagram in case["datagrams"]]
                for stalls in (False, True):
                    with self.subTest(peer="stalled" if stalls else "closed"):
                        self.check(case["expect"], verdict(datagrams, stalls))

    def check(self, expect: dict[str, Any], got: dict[str, Any]) -> None:
        self.assertEqual(got["outcome"], expect["outcome"], got.get("detail"))

        match expect["outcome"]:
            case "accept":
                self.check_accepted(expect, got)
            case "reject":
                self.assertEqual(got["error"], expect["error"])
            case "incomplete":
                self.assertTrue(got["detail"])
            case other:
                self.fail(f"the corpus grew an outcome this suite does not know: {other}")

    def check_accepted(self, expect: dict[str, Any], got: dict[str, Any]) -> None:
        control, payload = got["control"], got["payload"]
        if "control" in expect:
            self.assertEqual(control, expect["control"])
        for field, value in expect.get("control_shape", {}).items():
            self.assertIn(field, SHAPE, "no rule for this control_shape field")
            self.assertEqual(SHAPE[field](control), value, field)

        self.assertEqual(len(payload), expect["payload_len"])
        self.assertEqual(hashlib.sha256(payload).hexdigest(), expect["payload_sha256"])

        # What we decode, we must be able to send: the encoder chunks both sections the
        # same way, so an accepted case has to survive a round trip through it.
        again = verdict(encode_message(control, payload))
        self.assertEqual(again["outcome"], "accept", again.get("detail"))
        self.assertEqual(again["control"], control)
        self.assertEqual(again["payload"], payload)


class EncoderCeilingTest(unittest.TestCase):
    """The other half of framing: what the encoder refuses to put on the wire.

    A ceiling breached on the way out has to be an error the sender sees, not a datagram
    the receiver has to reject -- by then the connection is already halfway through a
    message it can never finish.
    """

    def test_a_payload_over_the_ceiling_is_refused_before_a_single_datagram(self) -> None:
        with self.assertRaises(ProtocolError) as caught:
            encode_message({"op": "infer"}, bytes(MAX_PAYLOAD + 1))
        self.assertIn(str(MAX_PAYLOAD), str(caught.exception))

    def test_a_control_block_over_the_ceiling_is_refused(self) -> None:
        with self.assertRaises(ProtocolError) as caught:
            encode_message({"ok": True, "text": "x" * (MAX_CONTROL + 1)})
        self.assertIn(str(MAX_CONTROL), str(caught.exception))

    # Not a table row: it owns a socket pair and a peer that never reads.
    def test_a_peer_that_stops_reading_times_out_rather_than_hanging(self) -> None:
        sender, peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.addCleanup(sender.close)
        self.addCleanup(peer.close)

        # Far past SO_SNDBUF, and nothing on the other end is reading, so the send blocks.
        with self.assertRaises(Timeout):
            send_message(sender, {"op": "infer"}, bytes(4 * 1024 * 1024), timeout=0.25)


if __name__ == "__main__":
    unittest.main()
