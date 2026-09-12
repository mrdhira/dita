#!/usr/bin/env python3
"""Regenerate the DIP conformance corpus. Stdlib only, deterministic.

    make dip-corpus

Every case is a complete list of datagrams handed to a decoder, plus the outcome both
implementations must agree on. Datagrams are base64 so binary payloads survive JSON exactly.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

MAX_CHUNK = 64 * 1024
MAX_CONTROL = 8 * 1024 * 1024
MAX_PAYLOAD = 64 * 1024 * 1024

OUT = Path(__file__).resolve().parent / "framing.json"


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def prologue(control_len: int, payload_len: int, protocol: int | None = 2) -> bytes:
    head: dict = {}
    if protocol is not None:
        head["protocol"] = protocol
    head["control_len"] = control_len
    head["payload_len"] = payload_len
    return json.dumps(head).encode("utf-8")


def chunks(raw: bytes) -> list[bytes]:
    return [raw[i : i + MAX_CHUNK] for i in range(0, len(raw), MAX_CHUNK)]


def message(control: dict, payload: bytes = b"") -> list[bytes]:
    body = json.dumps(control).encode("utf-8")
    return [prologue(len(body), len(payload)), *chunks(body), *chunks(payload)]


def case(name: str, why: str, datagrams: list[bytes], **expect) -> dict:
    return {
        "name": name,
        "why": why,
        "datagrams": [b64(d) for d in datagrams],
        "expect": expect,
    }


def payload_expect(raw: bytes) -> dict:
    """Assert a payload by length and digest rather than echoing the bytes.

    Keeps the corpus reviewable: the bulk cases exist to exceed a limit, and storing them
    twice would make the file large without making the assertion stronger.
    """
    return {
        "payload_len": len(raw),
        "payload_sha256": hashlib.sha256(raw).hexdigest(),
    }


def build() -> dict:
    cases: list[dict] = []

    # --- accepted -------------------------------------------------------------------
    cases.append(
        case(
            "control only",
            "the common case: an op with no payload",
            message({"op": "list"}),
            outcome="accept",
            control={"op": "list"},
            **payload_expect(b""),
        )
    )
    tiny = b"\x89PNG\r\n\x1a\n tiny"
    cases.append(
        case(
            "control and a small payload",
            "one payload datagram",
            message({"op": "infer"}, tiny),
            outcome="accept",
            control={"op": "infer"},
            **payload_expect(tiny),
        )
    )
    big = bytes(range(256)) * 275  # 70400 bytes: two datagrams, so chunking is exercised
    assert len(big) > MAX_CHUNK, "the point of this case is to span datagrams"
    cases.append(
        case(
            "payload spanning several datagrams",
            f"{len(big)} bytes is past max_chunk, which is why the payload is chunked",
            message({"op": "infer"}, big),
            outcome="accept",
            control={"op": "infer"},
            **payload_expect(big),
        )
    )
    # Sized to exceed SO_SNDBUF (212992), which is the defect this case guards.
    wide = {"ok": True, "lines": [{"text": "x" * 40, "n": i} for i in range(3600)]}
    wide_encoded = json.dumps(wide).encode("utf-8")
    assert len(wide_encoded) > 212992, f"only {len(wide_encoded)} bytes, must exceed SO_SNDBUF"
    cases.append(
        case(
            "control block spanning several datagrams",
            f"{len(wide_encoded)} bytes, past SO_SNDBUF: the defect that forced chunking the control block too",
            message(wide),
            outcome="accept",
            # Structural rather than deep: exact equality is already proven by the small
            # cases, and echoing 220 KB of JSON would not make this assertion stronger.
            control_shape={
                "control_len": len(wide_encoded),
                "lines": len(wide["lines"]),
                "first_line_text": wide["lines"][0]["text"],
                "last_line_n": wide["lines"][-1]["n"],
            },
            **payload_expect(b""),
        )
    )
    cases.append(
        case(
            "non-ascii control block",
            "UTF-8 must survive the length arithmetic, which counts bytes not characters",
            message({"op": "infer", "note": "日本語のテキスト認識"}),
            outcome="accept",
            control={"op": "infer", "note": "日本語のテキスト認識"},
            **payload_expect(b""),
        )
    )
    cases.append(
        case(
            "prologue without an explicit protocol field",
            "protocol is optional on the wire; absent means the current version",
            [prologue(len(b'{"op":"list"}'), 0, protocol=None), b'{"op":"list"}'],
            outcome="accept",
            control={"op": "list"},
            **payload_expect(b""),
        )
    )

    # --- rejected -------------------------------------------------------------------
    cases.append(
        case(
            "prologue is not utf-8 json",
            "a peer speaking something else must be told, not guessed at",
            [b"\xff\xfe not json"],
            outcome="reject",
            error="bad_request",
        )
    )
    cases.append(
        case(
            "prologue is a json array",
            "shape matters, not just parseability",
            [b"[1, 2, 3]"],
            outcome="reject",
            error="bad_request",
        )
    )
    cases.append(
        case(
            "control block is not json",
            "the prologue was fine, the body is not",
            [prologue(9, 0), b"not json!"],
            outcome="reject",
            error="bad_request",
        )
    )
    cases.append(
        case(
            "control block is a json array",
            "a control block must be an object",
            [prologue(9, 0), b"[1, 2, 3]"],
            outcome="reject",
            error="bad_request",
        )
    )
    cases.append(
        case(
            "control_len of zero",
            "every message has a control block; zero means the peer is confused",
            [prologue(0, 0)],
            outcome="reject",
            error="bad_request",
        )
    )
    cases.append(
        case(
            "negative control_len",
            "a length must be a non-negative integer",
            [b'{"protocol": 2, "control_len": -1, "payload_len": 0}'],
            outcome="reject",
            error="bad_request",
        )
    )
    cases.append(
        case(
            "control_len is a boolean",
            "json true is an int in some languages; it is not a length",
            [b'{"protocol": 2, "control_len": true, "payload_len": 0}'],
            outcome="reject",
            error="bad_request",
        )
    )
    cases.append(
        case(
            "control_len past max_control",
            "announced, not sent: refuse before allocating",
            [prologue(MAX_CONTROL + 1, 0)],
            outcome="reject",
            error="bad_request",
        )
    )
    cases.append(
        case(
            "payload_len past max_payload",
            "same, for the payload ceiling",
            [prologue(13, MAX_PAYLOAD + 1)],
            outcome="reject",
            error="bad_request",
        )
    )
    cases.append(
        case(
            "a datagram larger than max_chunk",
            "the sender ignored the chunk limit; MSG_TRUNC catches it rather than silence",
            [prologue(MAX_CHUNK + 10, 0), b"x" * (MAX_CHUNK + 10)],
            outcome="reject",
            error="bad_request",
        )
    )
    cases.append(
        case(
            "more control bytes than announced",
            "the sender and the prologue disagree",
            [prologue(5, 0), b"much longer than five"],
            outcome="reject",
            error="bad_request",
        )
    )
    cases.append(
        case(
            "prologue larger than the prologue ceiling",
            "the first datagram is meant to be small and fixed-shape",
            [b'{"protocol": 2, "control_len": 1, "payload_len": 0, "pad": "' + b"x" * 5000 + b'"}'],
            outcome="reject",
            error="bad_request",
        )
    )

    # --- incomplete -----------------------------------------------------------------
    cases.append(
        case(
            "fewer control datagrams than announced",
            "a stalled or truncated sender; a socket surfaces this as a timeout or a close",
            [prologue(40, 0), b"only thirteen"],
            outcome="incomplete",
        )
    )
    cases.append(
        case(
            "payload announced but never sent",
            "the classic half-sent message",
            [prologue(13, 4 * MAX_CHUNK), b'{"op":"infer"}'[:13]],
            outcome="incomplete",
        )
    )
    cases.append(
        case(
            "nothing at all",
            "an empty read means the peer closed",
            [],
            outcome="incomplete",
        )
    )

    return {
        "corpus": "dip-framing",
        "protocol": 2,
        "limits": {
            "max_chunk": MAX_CHUNK,
            "max_control": MAX_CONTROL,
            "max_payload": MAX_PAYLOAD,
        },
        "assertions": {
            "control": "deep equality with the decoded control block",
            "control_shape": "structural facts, used where echoing the block would only make the corpus large",
            "payload_len": "byte length of the decoded payload",
            "payload_sha256": "sha256 of the decoded payload",
        },
        "outcomes": {
            "accept": "decodes to the given control block and payload",
            "reject": "refused with the given error code",
            "incomplete": "the datagrams end mid-message; a socket implementation surfaces this as a timeout or a closed peer",
        },
        "cases": cases,
    }


if __name__ == "__main__":
    OUT.write_text(json.dumps(build(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    corpus = json.loads(OUT.read_text(encoding="utf-8"))
    counts: dict[str, int] = {}
    for entry in corpus["cases"]:
        counts[entry["expect"]["outcome"]] = counts.get(entry["expect"]["outcome"], 0) + 1
    print(f"{OUT.name}: {len(corpus['cases'])} cases {counts}")
