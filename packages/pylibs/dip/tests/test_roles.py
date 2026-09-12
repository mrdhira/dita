"""Both roles over real sockets.

Scenarios rather than a table: each one owns a socket pair or a listener and a thread, and
fails in its own way. The corpus proves what the bytes mean; these prove that the two ends
of this package actually talk to each other.
"""

from __future__ import annotations

import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any

import dip


def stub_handler(control: dict[str, Any], payload: bytes) -> dict[str, Any]:
    """A receiver with no model in it: validate, then answer from the control block."""
    refusal = dip.validate(control)
    if refusal is not None:
        return refusal
    match control["op"]:
        case "handshake" | "version":
            return dip.ok(service="stub", version="0", protocol=dip.PROTOCOL_VERSION, limits=dip.limits())
        case "infer":
            return dip.ok(text=payload.decode("ascii"), lines=[], model="stub", infer_ms=0.0)
        case "load":
            return dip.ok(id=control.get("id") or control.get("model"), unloaded=None)
        case op:
            return dip.ok(op=op)


class ReceiverTest(unittest.TestCase):
    def serve(self, handler: dip.Handler = stub_handler, **timeouts: float) -> socket.socket:
        """Start `serve_connection` on one end of a pair and hand back the other."""
        client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.addCleanup(server.close)

        thread = threading.Thread(
            target=dip.serve_connection, args=(server, handler), kwargs=timeouts, daemon=True
        )
        thread.start()
        # Cleanups run last-registered-first: close the peer so the loop ends, then join it.
        self.addCleanup(thread.join, 10)
        self.addCleanup(client.close)
        return client

    def test_a_request_is_answered_and_the_loop_ends_when_the_peer_closes(self) -> None:
        client = self.serve()
        dip.send_message(client, {"op": "list"})
        response, payload = dip.recv_message(client, 10.0, 10.0)

        self.assertEqual(response, {"ok": True, "op": "list"})
        self.assertEqual(payload, b"")

    def test_a_malformed_message_is_refused_with_a_code_not_a_reset(self) -> None:
        client = self.serve()
        client.send(b"\xff not json")

        response, _payload = dip.recv_message(client, 10.0, 10.0)
        self.assertEqual(response["error"]["code"], dip.ErrorCode.bad_request)

    def test_a_response_over_the_ceiling_is_answered_rather_than_dropped(self) -> None:
        """The peer is owed an answer even when the answer is the thing that is wrong."""
        huge = dip.ok(text="x" * (dip.MAX_CONTROL + 1))
        client = self.serve(lambda control, payload: huge)

        with self.assertLogs("dip.receiver", level="WARNING"):
            dip.send_message(client, {"op": "infer"}, b"png")
            response, _payload = dip.recv_message(client, 10.0, 10.0)
        self.assertEqual(response["error"]["code"], dip.ErrorCode.response_too_large)

    def test_a_stalled_peer_is_told_it_timed_out(self) -> None:
        client = self.serve(message_timeout=0.25)
        control = json.dumps({"op": "infer"}).encode("utf-8")
        client.send(
            json.dumps(
                {
                    "protocol": dip.PROTOCOL_VERSION,
                    "control_len": len(control),
                    "payload_len": 4 * dip.MAX_CHUNK,
                }
            ).encode("utf-8")
        )
        client.send(control)
        client.send(b"z" * dip.MAX_CHUNK)  # one of the four announced chunks, then silence

        response, _payload = dip.recv_message(client, 10.0, 10.0)
        self.assertEqual(response["error"]["code"], dip.ErrorCode.timeout)

    def test_a_refused_peer_reads_its_answer_before_the_close(self) -> None:
        """Closing a SEQPACKET socket with data queued resets it; the peer needs the code."""
        client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.addCleanup(client.close)

        # A requester hands over its first op immediately, so the refusal has to drain it.
        dip.send_message(client, {"op": "handshake"})
        dip.refuse(server, dip.error(dip.ErrorCode.busy, "16 connections are already open"), 0.1)

        response, _payload = dip.recv_message(client, 10.0, 10.0)
        self.assertEqual(response["error"]["code"], dip.ErrorCode.busy)

    def test_a_connection_that_breaks_mid_read_ends_the_loop(self) -> None:
        """An OSError from the socket is the peer going away, not a protocol failure."""
        _client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        server.close()

        with self.assertNoLogs("dip.receiver", level="WARNING"):
            dip.serve_connection(server, stub_handler)

    def test_an_answer_to_a_peer_that_has_gone_is_dropped_quietly(self) -> None:
        """Both ways a send can fail on a dead peer: a plain response, and one over the
        ceiling whose `response_too_large` cannot be delivered either."""
        client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.addCleanup(server.close)
        client.close()

        self.assertFalse(dip.send_response(server, dip.ok(op="list")))
        with self.assertLogs("dip.receiver", level="WARNING"):
            self.assertFalse(dip.send_response(server, dip.ok(text="x" * (dip.MAX_CONTROL + 1))))

    def test_keep_going_stops_the_loop_between_messages(self) -> None:
        client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.addCleanup(client.close)
        self.addCleanup(server.close)

        dip.serve_connection(server, stub_handler, keep_going=lambda: False)
        # Nothing was read, so the request is still queued: the loop never ran.
        dip.send_message(client, {"op": "list"})
        self.assertEqual(dip.recv_message(server, 10.0, 10.0)[0], {"op": "list"})


class RequesterTest(unittest.TestCase):
    """The requesting end against a listening receiver, one connection per test."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "dip.sock"

        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.listener.bind(str(self.path))
        self.listener.listen(1)
        self.addCleanup(self.listener.close)

    def accept_one(self) -> None:
        connection, _address = self.listener.accept()
        with connection:
            dip.serve_connection(connection, stub_handler, 10.0, 10.0)

    def client(self) -> dip.Requester:
        """One receiver waiting for one connection. Started here, not in setUp, so a test
        that never dials leaves no thread parked in accept."""
        thread = threading.Thread(target=self.accept_one, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 10)

        requester = dip.Requester.connect(self.path, 10.0)
        self.addCleanup(requester.close)
        return requester

    def test_the_ops_round_trip_over_one_connection(self) -> None:
        with self.client() as requester:
            self.assertEqual(requester.handshake()["protocol"], dip.PROTOCOL_VERSION)
            self.assertTrue(requester.list_models()["ok"])
            self.assertEqual(requester.load("rapidocr-ppocrv5")["id"], "rapidocr-ppocrv5")
            self.assertTrue(requester.unload()["ok"])
            self.assertEqual(requester.probe("readyz")["op"], "readyz")

    def test_infer_carries_the_image_as_the_payload(self) -> None:
        """The image rides in the payload, and `infer` names no model."""
        requester = self.client()
        self.assertEqual(requester.infer(b"pretend png")["text"], "pretend png")

        refused = requester.call("infer", b"pretend png", model="manga-ocr")
        self.assertEqual(refused["error"]["code"], dip.ErrorCode.bad_request)

    def test_connecting_to_nothing_raises_rather_than_hanging(self) -> None:
        with self.assertRaises(OSError):
            dip.Requester.connect(self.path.with_name("absent.sock"), 1.0)


if __name__ == "__main__":
    unittest.main()
