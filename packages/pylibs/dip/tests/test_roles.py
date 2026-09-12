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


def drain(sock: socket.socket) -> list[bytes]:
    """Every datagram queued on this end right now, in order."""
    out = []
    sock.settimeout(0.25)
    try:
        while True:
            datagram = sock.recv(dip.MAX_CHUNK)
            if not datagram:
                return out
            out.append(datagram)
    except (TimeoutError, OSError):
        return out


class StallingSocket:
    """Enough socket for `send_message`: it takes datagrams until the peer's buffer is
    full, then times out the way a blocking send on a full SO_SNDBUF does.

    A real socket cannot show what happens next. Once its buffer is full, the fallback
    send blocks too, so the bug this stands in for -- a refusal appended to a message the
    peer is still reading -- is invisible unless the peer resumes, which is exactly what
    `recovers` does.
    """

    def __init__(self, stall_at: int, recovers: bool = True) -> None:
        self.sent: list[bytes] = []
        self._stall_at = stall_at
        self._recovers = recovers
        self._stalled = False
        self._timeout: float | None = None

    def gettimeout(self) -> float | None:
        return self._timeout

    def settimeout(self, value: float | None) -> None:
        self._timeout = value

    def send(self, datagram: bytes) -> int:
        if len(self.sent) >= self._stall_at and not (self._stalled and self._recovers):
            self._stalled = True
            raise TimeoutError("timed out")
        self.sent.append(datagram)
        return len(datagram)


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
        client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.addCleanup(client.close)
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

    def test_a_peer_that_stopped_reading_is_told_nothing_more(self) -> None:
        """A send that times out has left half a message on the wire. Appending a refusal
        to it would be read as the rest of that message, so there is nothing to say -- and
        the timeout must not escape either: it is not an OSError, and out of a connection
        thread it is a traceback rather than a closed socket."""
        client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.addCleanup(client.close)
        self.addCleanup(server.close)

        # Far past SO_SNDBUF, and the client never reads, so the send stalls part-way.
        response = dip.ok(text="x" * (2 * 1024 * 1024))
        intended = dip.encode_message(response)

        with self.assertLogs("dip.receiver", level="WARNING") as logged:
            self.assertFalse(dip.send_response(server, response, send_timeout=0.25))

        arrived = drain(client)
        self.assertGreater(len(arrived), 0, "nothing was sent at all; the fixture is wrong")
        self.assertLess(len(arrived), len(intended), "the send did not stall; raise the size")
        # Every datagram the peer can read is a prefix of the one message we started, with
        # no second message appended to it.
        self.assertEqual(arrived, intended[: len(arrived)])
        # The wording matters: `str(Timeout)` is "peer stopped reading" either way, so a
        # looser assertion here would pass with this branch folded back into the one that
        # calls a timed-out send `response_too_large`.
        self.assertIn("peer stopped reading a response", logged.output[0])
        self.assertNotIn("response_too_large", "".join(logged.output))

    def test_a_refusal_is_never_appended_to_a_half_sent_message(self) -> None:
        """The interesting peer is the slow one, not the dead one: it starts reading again
        just after the send gave up, so a refusal sent now is accepted by the kernel and
        read as the rest of the message it was appended to."""
        stalled = StallingSocket(stall_at=3)
        response = dip.ok(text="x" * (4 * dip.MAX_CHUNK))
        intended = dip.encode_message(response)
        self.assertGreater(len(intended), 4, "the fixture must span more than the stall point")

        with self.assertLogs("dip.receiver", level="WARNING"):
            self.assertFalse(dip.send_response(stalled, response, send_timeout=0.25))

        self.assertEqual(stalled.sent, intended[:3], "something was appended to half a message")

    def test_a_refusal_that_also_times_out_does_not_escape(self) -> None:
        """`Timeout` is not an `OSError`. Out of `send_response` it leaves `serve_connection`
        and kills the connection thread with a traceback instead of closing a socket."""
        stalled = StallingSocket(stall_at=1, recovers=False)

        with self.assertLogs("dip.receiver", level="WARNING"):
            self.assertFalse(dip.send_response(stalled, dip.ok(op="list"), send_timeout=0.25))

    def test_keep_going_stops_the_loop_between_messages(self) -> None:
        client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.addCleanup(client.close)
        self.addCleanup(server.close)

        dip.serve_connection(server, stub_handler, keep_going=lambda: False)
        # Nothing was read, so the request is still queued: the loop never ran.
        dip.send_message(client, {"op": "list"})
        self.assertEqual(dip.recv_message(server, 10.0, 10.0)[0], {"op": "list"})


class NegotiatedLimitsTest(unittest.TestCase):
    """A requester frames with what the peer advertised, not with this package's defaults.

    The receiver here advertises a chunk limit deliberately unlike ours, and enforces it:
    its reader refuses a datagram over 4096 bytes, so a requester that kept sending 64 KiB
    chunks is refused rather than quietly tolerated.
    """

    SMALL = dip.Limits(max_chunk=4096, max_control=1 << 20, max_payload=1 << 20)

    def setUp(self) -> None:
        self.advertised_protocol = dip.PROTOCOL_VERSION
        self.refuse_handshake = False
        self.advertise_limits = True
        self.assertNotEqual(
            self.SMALL.max_chunk, dip.MAX_CHUNK, "the fake must not advertise our own default"
        )
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "small.sock"

        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.listener.bind(str(self.path))
        self.listener.listen(1)
        self.addCleanup(self.listener.close)

        thread = threading.Thread(target=self.accept_one, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 10)

    def accept_one(self) -> None:
        connection, _address = self.listener.accept()
        with connection:
            dip.serve_connection(connection, self.handle, 10.0, 10.0, limits=self.SMALL)

    def handle(self, control: dict[str, Any], payload: bytes) -> dict[str, Any]:
        refusal = dip.validate(control)
        if refusal is not None:
            return refusal
        if control["op"] in ("handshake", "version"):
            return self.handshake_response
        return dip.ok(payload_len=len(payload))

    @property
    def handshake_response(self) -> dict[str, Any]:
        if self.refuse_handshake:
            return dip.error(dip.ErrorCode.busy, "not right now")
        described = dip.ok(
            service="small", version="0", protocol=self.advertised_protocol,
        )
        return {**described, "limits": self.SMALL.as_dict()} if self.advertise_limits else described

    def client(self) -> dip.Requester:
        requester = dip.Requester.connect(self.path, 10.0)
        self.addCleanup(requester.close)
        return requester

    def test_a_handshaked_requester_chunks_to_the_peers_limit(self) -> None:
        requester = self.client()
        self.assertEqual(requester.limits.max_chunk, dip.MAX_CHUNK)

        requester.handshake()
        self.assertEqual(requester.limits.max_chunk, self.SMALL.max_chunk)

        image = b"z" * (5 * self.SMALL.max_chunk)
        answer = requester.infer(image)
        self.assertTrue(answer["ok"], answer.get("error"))
        self.assertEqual(answer["payload_len"], len(image))

    def test_the_receiver_really_does_refuse_an_oversized_datagram(self) -> None:
        """Anti-vacuity for the test above: without the handshake the same call is refused,
        so that one passes because the limit was adopted rather than because nothing checks."""
        requester = self.client()
        refused = requester.infer(b"z" * (5 * self.SMALL.max_chunk))
        self.assertEqual(refused["error"]["code"], dip.ErrorCode.bad_request)
        self.assertIn(str(self.SMALL.max_chunk), refused["error"]["message"])


    def test_a_handshake_that_says_nothing_about_limits_leaves_ours_alone(self) -> None:
        """Two ways a handshake carries no limits: the peer refused to answer it, and the
        peer answered without them. Neither may leave the connection framing with zeroes."""
        requester = self.client()

        self.refuse_handshake = True
        refused = requester.handshake()
        self.assertEqual(refused["error"]["code"], dip.ErrorCode.busy)
        self.assertEqual(requester.limits, dip.DEFAULT_LIMITS)

        self.refuse_handshake, self.advertise_limits = False, False
        self.assertTrue(requester.handshake()["ok"])
        self.assertEqual(requester.limits, dip.DEFAULT_LIMITS)

    def test_a_peer_on_another_wire_version_is_not_talked_to(self) -> None:
        """Every op after the handshake would be framed against a guess, so this is the one
        place in the requester that raises rather than answering."""
        self.advertised_protocol = dip.PROTOCOL_VERSION + 97
        requester = self.client()

        with self.assertRaises(dip.ProtocolError) as caught:
            requester.handshake()
        self.assertIn(str(dip.PROTOCOL_VERSION + 97), str(caught.exception))
        self.assertEqual(requester.limits, dip.DEFAULT_LIMITS, "nothing was adopted from it")


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
