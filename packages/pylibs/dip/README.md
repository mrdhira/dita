# dip

> The Dita Inference Protocol in Python: the framing, the error taxonomy, and both ends of
> the wire. Stdlib only, so linking it costs nothing but this directory.

**Status: v0.** Used by `services/inferences-ocr`, which is where this code came from. The
Go half is `packages/golibs/dip`; the two are kept honest by one corpus, not by review.

- **[`specs/dip/dip.schema.json`](../../../specs/dip/dip.schema.json) is the IDL** and
  `src/dip/types.py` is generated from it — do not hand-edit, run `make dip-generate`
- **[`docs/protocol/[1]dip-specification.md`](../../../docs/protocol/%5B1%5Ddip-specification.md)**
  is the prose: why the protocol exists, and every behaviour a schema cannot carry
- **Zero third-party imports**, an acceptance criterion rather than a preference, proved by
  `make dip-verify` scanning every import against `sys.stdlib_module_names`

## What is in it

| module | what it owns |
| --- | --- |
| `framing.py` | the prologue, the chunked control block, the chunked payload, the ceilings and the timeouts |
| `errors.py` | `ok()` / `error()`, and `ErrorCode` re-exported from the generated types |
| `ops.py` | which ops exist, which fields each declares, and the refusal of anything else |
| `receiver.py` | the exchange on one accepted connection: read, answer, refuse |
| `requester.py` | the other end: connect, handshake, perform ops |
| `types.py` | generated dataclasses, one per shape in the IDL |

Binding, the accept loop and any connection cap stay with the service: those are policy,
and a receiver owns them. What is here is what both implementations must agree on.

## Using it

```python
import dip

with dip.Requester.connect("/run/dita/inferences-ocr.sock") as worker:
    print(worker.handshake()["limits"])        # adopted: `worker.limits` frames every call
    worker.load("rapidocr-ppocrv5")
    result = worker.infer(open("page.png", "rb").read())
    print(result["text"] if result["ok"] else result["error"]["code"])
```

The receiving end is a handler and a connection; the service keeps its own accept loop:

```python
def handle(control, payload):
    return dip.validate(control) or dip.ok(**do_the_work(control, payload))

dip.serve_connection(connection, handle, idle_timeout, message_timeout)
```

A failure is a response, not an exception — branch on `error.code`, log `error.message`.
Only a connection that breaks raises: `ProtocolError`, `Timeout`, `PeerGone`.

**Nothing on the wire is hard-coded.** `handshake` checks the peer's `protocol` and adopts
the `limits` it advertises; `Limits` is then passed to every framing call, so a receiver
with a smaller `max_chunk` is chunked to, not sent 64 KiB and hoped at. `DEFAULT_LIMITS` is
only what to frame with until the handshake has answered. A receiver advertises its own by
passing them to `serve_connection`.

## Tests

```bash
make test
make coverage        # the same suite under coverage, with a floor
```

The suite is the conformance corpus plus the sockets. `tests/test_framing.py` and
`tests/test_dispatch.py` read every case out of
[`specs/dip/conformance/`](../../../specs/dip/conformance/) — one `subTest` per case, named
by the case name, so adding a case there needs no edit here. Framing cases are run twice,
once against a peer that closed and once against a peer that stalled, because the corpus
calls both `incomplete`. `tests/test_roles.py` is scenarios over real socket pairs: the two
roles talking to each other, a stalled peer, a response past the ceiling, a refusal, and a
receiver advertising a chunk limit smaller than this package's own.
