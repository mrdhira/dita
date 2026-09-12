---
type: specification
status: in-review
owner: Dhira Wigata
product: dita
prd: n/a — internal protocol
date: 2026-09-12
tags:
  - dita
  - protocol
  - dip
---

# DIP — the Dita Inference Protocol, version `dip/2`

> The IDL is [`specs/dip/dip.schema.json`](../../specs/dip/dip.schema.json) and it is the
> source of truth for every shape below. This document is the source of truth for everything
> a schema cannot express: why the protocol exists, what the framing means, and which
> behaviours are settled.

## What DIP is

A request/response protocol for inference workers on the same host. One side requests, the
other receives; a message is a JSON control block plus an optional opaque payload, carried
over an `AF_UNIX` `SOCK_SEQPACKET` socket.

It exists to move an image to a worker and a transcription back, tens of times a second at
most, between two processes that share a kernel. It is not a general RPC system, it is not
networked, and it has no ambition to become either.

## Why a new protocol

Inventing a wire format is usually the wrong instinct, so this section is the justification
rather than an afterthought. The requirement that decides it: **an efficient path for a
multi-megabyte opaque payload alongside a small structured control block, with no dependency
in either implementation.**

**gRPC / protobuf — rejected.** It brings a compiler toolchain and a runtime library into
both languages, to serialise a control block that is a dozen fields of JSON, for a call that
never leaves the machine. The payload would ride as a `bytes` field, which protobuf handles
well, but the cost is a permanent dependency on both sides and a code generation step that
produces code depending on that runtime. For a same-host call, HTTP/2 framing, stream
multiplexing and flow control are all machinery we pay for and do not use.

**Cap'n Proto / FlatBuffers — rejected, for the same reason with a different shape.** Both
are excellent at zero-copy reads of large structured messages. Our large thing is opaque
bytes, which neither format improves: a PNG is a PNG. What remains is codegen and a runtime
library for the small structured part, which is the part we can already write by hand in
forty lines.

**Newline-delimited JSON — rejected.** Attractive for the control block, and the obvious
choice if that were all we sent. It has no efficient binary path: the payload has to be
base64'd into the JSON, costing a third more bytes and a full encode and decode on both
sides, or else framed out-of-band, at which point the framing is the protocol and NDJSON is
just the encoding of one part of it. Which is, in fact, what DIP is.

**HTTP with multipart — rejected.** This is the version most reviewers would reach for, and
it was the shape the worker briefly had. It means a TCP listener or a unix HTTP server, an
HTTP parser, a multipart parser, and a server dependency such as FastAPI or uvicorn, on both
ends, to move bytes between two processes on one host. Filesystem permissions on a socket
replace an auth scheme. The measured numbers put the framing overhead in proportion: a
full-page inference is 1.0 to 1.6 seconds, so HTTP's cost is noise — the objection is not
latency, it is the dependency, the second transport to secure, and the parsing surface.

**What is left is small.** `SOCK_SEQPACKET` gives message boundaries for free, which is most
of what a framing layer does. JSON gives the control block a schema-describable shape that
every language reads with its standard library. The payload is bytes and stays bytes. The
whole protocol is a prologue, two lengths, and a chunking rule.

**The honest cost:** a client implements roughly forty lines of framing, and we own the
conformance tests that keep two implementations agreeing. That is the trade, stated plainly
so a reviewer can disagree with it.

## Transport and framing

**Transport.** `AF_UNIX` / `SOCK_SEQPACKET`, bound at a filesystem path with mode `0660`.
Message boundaries and ordering are preserved by the kernel, so a message needs no length
prefix — only a count of the bytes that follow it. Access control is filesystem permissions;
there are no tokens.

**Framing.** Every message is:

```
datagram 0     : the prologue — {"protocol": 2, "control_len": N, "payload_len": M}
next datagrams : the control block, in chunks of at most max_chunk bytes
next datagrams : the payload, in chunks of at most max_chunk bytes
```

A length of zero means that section sends no datagrams at all. No conforming datagram is
ever empty, so an empty read means the peer closed.

**Both sections are chunked, and this is the part implementers get wrong.** A single AF_UNIX
datagram cannot exceed `SO_SNDBUF` — 212992 bytes on a default Linux kernel. Above that,
`send` fails with `EMSGSIZE`; it does not fragment. That applies to the control block just as
much as to the payload: a dense page yields 900 detected lines and a 221 KB response, which
exceeds the ceiling. An implementation that chunks only the payload works until the first
busy page and then drops the connection with no error code.

The receive buffer is exactly `max_chunk`. A peer sending a larger datagram is caught by
`MSG_TRUNC` and answered `bad_request`, rather than being silently truncated.

**Limits**, advertised by `handshake` under `limits` so nothing is hard-coded:

| field | value in this implementation | meaning |
| --- | --- | --- |
| `max_chunk` | 65536 | largest datagram either side may send |
| `max_control` | 8388608 | largest control block, roughly 70000 lines |
| `max_payload` | 67108864 | largest payload |
| `idle_timeout_s` | 300 | how long an open connection may sit between messages |
| `message_timeout_s` | 30 | how long a half-sent message may stall before it is dropped |

A receiver may additionally cap concurrent connections; this implementation accepts 16 and
answers `busy` beyond that, after a drain window so the refusal is readable.

## Ops

Field-level shapes are in the IDL. This table is the index.

| op | request | response |
| --- | --- | --- |
| `handshake`, `version` | no fields | `HandshakeResponse` |
| `list` | no fields | `ListResponse` |
| `load` | `id` (`model` accepted as an alias) | `LoadResponse` |
| `unload` | no fields | `UnloadResponse` |
| `infer` | no fields; the image is the payload | `InferResponse` |
| `livez`, `readyz`, `startupz` | no fields | `ProbeResponse` |

Every response carries `ok`. A failure is an `ErrorResponse`: `{"ok": false, "error":
{"code", "message"}}`. **`code` is the contract and `message` is prose** — branch on the code.

`checksum_mismatch` and `fetch_failed` mean the model is unusable; retrying `infer` will not
help, so retry the `load` or choose another model.

## Health

Three ops, not three URL paths, because there is no HTTP surface and adding one purely for
probes would mean a listener, a parser and a second transport to secure. The names follow the
Kubernetes convention so the semantics are the familiar ones; `healthz` is deprecated there
and is not offered here.

| probe | true when | false means |
| --- | --- | --- |
| `livez` | the process and its accept loop are up; no dependency checks | restart me |
| `readyz` | can be given work: bound, registry parsed, models dir writable, and either something is resident or a load could still proceed | stop routing to me |
| `startupz` | one-time boot finished | do not kill me, I am still booting |

A cold load does **not** make `readyz` false — a download in flight is progress, not a wedge.
`resident` in the response is what says whether an `infer` would succeed this instant.

A container healthcheck is an exec probe against the same ops.

## Roles

DIP is symmetric in principle: one side requests, one side receives, and a package that
implements it should support both directions. A worker is a receiver today and will be a
requester when it calls another worker.

**State of the implementations, plainly:**

| | `packages/pylibs/dip` | `packages/golibs/dip` |
| --- | --- | --- |
| requester | implemented | implemented |
| receiver | implemented, in production use by `services/inferences-ocr` | **designed and documented, not implemented** |

The Go receiver is not written because nothing needs it yet. Its shape is fixed by this
specification and by the conformance corpus, which both sides already run.

## Settled invariants

These are behaviours, not shapes, so the schema cannot carry them.

- **One model resident per receiver.** `load` releases the previous engine before building
  the next. `load` reports what it evicted in `unloaded`, always present. A memory budget is
  therefore the largest single model, not the sum.
- **`infer` takes no `model` field.** `load` first, `unload` after, so which model answered is
  never in doubt. A control block carrying `model` on `infer` is refused with `bad_request`
  and a message pointing at `load`.
- **Undeclared control fields are refused, not ignored.** Each op declares its fields; anything
  else is `bad_request`. Silent tolerance hides typos and turns a client bug into a server
  behaviour.
- **No implicit loading.** `infer` with nothing resident returns `no_model_loaded`. Choosing
  what is resident belongs to the requester, and a load can mean a multi-hundred-megabyte
  download.
- **A load downloads before it evicts.** While fetching, `loading` names the incoming model and
  `resident` still names the outgoing one, which keeps serving. A failed fetch leaves the
  working model in place.
- **The receiver owns no queue.** It holds one exclusive lock and serialises behind it. Rate
  limiting, retries, timeouts and backpressure belong to the requester.

## Versioning

The wire version is an integer in the prologue and in `handshake.protocol`. This is `dip/2`;
version 1 was the pre-chunking framing and no implementation speaks it.

**Discovery.** A requester sends `handshake` once per connection and reads `protocol`. A
receiver answering a version the requester does not know is a receiver it should not drive.
Limits come from the same response, so a peer never hard-codes a size.

**What forces a bump.** Any change that would make an existing peer misread a message:
altering the prologue, changing the chunking rule, removing an op, removing or retyping a
required field, or changing the meaning of an error code.

**What does not.** Adding an op, adding an optional field to a response, adding an error code,
or widening a limit. A requester must ignore response fields it does not recognise — this is
the one place tolerance is correct, and it is the opposite of the rule for request fields,
where the receiver refuses what it does not declare. The asymmetry is deliberate: a receiver
that tolerates unknown request fields hides client bugs, while a requester that refuses
unknown response fields cannot be upgraded independently.

## The IDL and code generation

The schema lives at [`specs/dip/dip.schema.json`](../../specs/dip/dip.schema.json), in
`specs/` rather than under either language, so neither implementation looks authoritative.
The conformance corpus sits beside it in
[`specs/dip/conformance/`](../../specs/dip/conformance/).

Types are generated, committed, and regenerated with:

```bash
make dip-generate
```

**Generated code has zero third-party dependencies**, which is an acceptance criterion rather
than a preference, and it is enforced by `make dip-verify`:

- Go — `go-jsonschema` emits structs plus `UnmarshalJSON` required-field checks, importing
  only `encoding/json`, `fmt` and `reflect`. `go list -deps` reports no module-path packages.
- Python — `datamodel-code-generator` in `dataclasses.dataclass` mode emits plain dataclasses
  importing only `dataclasses`, `enum` and `typing`.

Both generators are **dev-only tools**, pinned and invoked on demand. Neither reaches a
runtime image; the image build exports with `--no-default-groups`, which is load-bearing.

What is generated is *types*. The framing, the role implementations and the error taxonomy
are hand-written library code in each package, because they are protocol behaviour rather
than data shape. The conformance corpus is what keeps the two hand-written halves honest.
