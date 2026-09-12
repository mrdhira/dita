"""The framing a worker's responses actually go through.

`dip` owns the wire and has its own corpus; what is asserted here is the shape a worker
produces -- a dense page's control block, a payload of image bytes -- against the ceilings
and the timeouts it will meet in production. The EMSGSIZE regression below is the reason
the control block is chunked at all, and it was found by a worker, not by a corpus.

Scenarios rather than a table: each owns a socket pair and, in two cases, a clock.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import unittest
from typing import Any, Dict

import dip


class FramingTest(unittest.TestCase):
    def round_trip(self, control: Dict[str, Any], payload: bytes) -> tuple:
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        received: list = []
        reader = threading.Thread(target=lambda: received.append(dip.recv_message(right)))
        reader.start()
        dip.send_message(left, control, payload)
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
        payload = bytes(range(256)) * (dip.MAX_CHUNK // 64)
        self.assertGreater(len(payload), dip.MAX_CHUNK)

        decoded_control, decoded_payload = self.round_trip({"op": "infer"}, payload)
        self.assertEqual(decoded_control, {"op": "infer"})
        self.assertEqual(decoded_payload, payload)
        self.assertEqual(len(decoded_payload), len(payload))

    def test_a_prologue_that_is_not_utf8_json_is_rejected(self) -> None:
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        left.send(b"\xff not json")
        with self.assertRaises(dip.ProtocolError):
            dip.recv_message(right)
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
        reader = threading.Thread(target=lambda: received.append(dip.recv_message(right)))
        reader.start()
        dip.send_message(left, response)
        reader.join(timeout=10)

        self.assertEqual(received[0][0], response)

    def test_a_control_block_over_the_ceiling_is_refused_not_dropped(self) -> None:
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.addCleanup(left.close)
        self.addCleanup(right.close)

        oversized = {"ok": True, "text": "x" * (dip.MAX_CONTROL + 1)}
        with self.assertRaises(dip.ProtocolError):
            dip.send_message(left, oversized)

    def test_a_message_that_sends_fewer_datagrams_than_announced_times_out(self) -> None:
        """A peer that announces a payload and then stalls must not pin the reader."""
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.addCleanup(left.close)
        self.addCleanup(right.close)

        control = json.dumps({"op": "infer"}).encode("utf-8")
        left.send(
            json.dumps(
                {
                    "protocol": dip.PROTOCOL_VERSION,
                    "control_len": len(control),
                    "payload_len": 4 * dip.MAX_CHUNK,
                }
            ).encode("utf-8")
        )
        left.send(control)
        left.send(b"z" * dip.MAX_CHUNK)  # one of the four announced chunks, then silence

        started = time.monotonic()
        with self.assertRaises(dip.Timeout):
            dip.recv_message(right, idle_timeout=5.0, message_timeout=0.25)
        self.assertLess(time.monotonic() - started, 5.0)

    def test_a_peer_that_closes_mid_message_is_reported_as_gone(self) -> None:
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.addCleanup(right.close)

        control = json.dumps({"op": "infer"}).encode("utf-8")
        left.send(
            json.dumps(
                {
                    "protocol": dip.PROTOCOL_VERSION,
                    "control_len": len(control),
                    "payload_len": 99999,
                }
            ).encode("utf-8")
        )
        left.send(control)
        left.close()

        with self.assertRaises(dip.PeerGone):
            dip.recv_message(right, idle_timeout=5.0, message_timeout=5.0)

    def test_an_announced_length_over_the_ceiling_is_rejected(self) -> None:
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.addCleanup(left.close)
        self.addCleanup(right.close)

        left.send(
            json.dumps({"control_len": 10, "payload_len": dip.MAX_PAYLOAD + 1}).encode("utf-8")
        )
        with self.assertRaises(dip.ProtocolError):
            dip.recv_message(right, idle_timeout=5.0, message_timeout=5.0)

    def test_an_oversized_datagram_is_caught_rather_than_truncated(self) -> None:
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.addCleanup(left.close)
        self.addCleanup(right.close)

        left.send(b"{" + b" " * (dip.RECV_BUFFER * 2))
        with self.assertRaises(dip.ProtocolError) as caught:
            dip.recv_message(right, idle_timeout=5.0)
        self.assertIn("chunk limit", str(caught.exception))

    def test_a_json_array_control_block_is_rejected(self) -> None:
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.addCleanup(left.close)
        self.addCleanup(right.close)

        body = b"[1, 2, 3]"
        left.send(
            json.dumps(
                {"protocol": dip.PROTOCOL_VERSION, "control_len": len(body), "payload_len": 0}
            ).encode("utf-8")
        )
        left.send(body)
        with self.assertRaises(dip.ProtocolError) as caught:
            dip.recv_message(right, idle_timeout=5.0)
        self.assertIn("must be a JSON object", str(caught.exception))
