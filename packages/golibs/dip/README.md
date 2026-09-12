# dip — the Go half of the Dita Inference Protocol

> Dial a worker's unix socket, handshake, and drive it. Standard library only, and the
> conformance corpus is what keeps this honest against the Python implementation.

`dip/2` is a JSON control block plus an optional opaque payload over `AF_UNIX`
`SOCK_SEQPACKET`. The protocol is in [`docs/protocol/[1]dip-specification.md`](../../../docs/protocol/%5B1%5Ddip-specification.md),
the IDL is [`specs/dip/dip.schema.json`](../../../specs/dip/dip.schema.json), and the corpus
both languages run is [`specs/dip/conformance/`](../../../specs/dip/conformance).

## Use it

```go
requester, err := dip.Dial(ctx, "/run/dita/inferences-ocr.sock")
defer requester.Close()

hello, err := requester.Handshake(ctx)   // once per connection: version and limits
_, err = requester.Load(ctx, hello.DefaultModel)
result, err := requester.Infer(ctx, pixels)
fmt.Println(result.Text)
```

`services/inferences-ocr/examples/go` is the whole contract end to end, and prints what came
back from a live worker.

## What is here

| file | what it is |
| --- | --- |
| `types.go` | **Generated** from the IDL by `make dip-generate` at the repo root. Do not edit. |
| `framing.go` | The prologue, the chunking rule, the ceilings, the error taxonomy, and the dispatch table. |
| `requester.go` | The requester role: dial, handshake, list, load, unload, infer, and the three health probes. |
| `receiver.go` | The receiver role, documented and deliberately not implemented — nothing in Go listens yet. |
| `conformance_test.go` | Reads `framing.json` and `dispatch.json` from the shared corpus and asserts every case. |
| `responses_test.go` | Reads `responses.json` and decodes every case with the generated types. |

## Four things this package exists to get right

**One `Write` per datagram, and never over `max_chunk`.** `unixpacket` is `SOCK_SEQPACKET`,
so the kernel keeps message boundaries — wrapping the connection in a `bufio.Writer` would
merge datagrams and destroy the framing. Both the control block and the payload are chunked,
because an AF_UNIX datagram cannot exceed `SO_SNDBUF` and a dense page's result runs past it.

**Nothing is hard-coded.** `Handshake` checks the wire version and adopts the peer's
`limits`; `DefaultLimits` is only what to frame with until it has answered.

**`error.code` is the contract.** `CodeOf(err)` is the branch point — `checksum_mismatch`
and `fetch_failed` mean retrying the `infer` will not help. `error.message` is prose.
A health probe answering `ok: false` is not an error, it is a verdict, and it comes back as
a `ProbeResponse` with the reasons it failed.

**A failed exchange retires the connection.** There is no request id on this wire, so an
answer is matched to a question by being next. A call that ends early -- a deadline, a
cancellation, a short write -- leaves the peer's answer queued on the socket, and reusing
that connection would return the previous call's result for the current call's question. So
a failed exchange closes the connection, and every later call on it fails with
`dip.ErrConnectionRetired`. A refusal carrying an `error.code` is an answer, not a failure,
and leaves the connection usable. **Retrying means dialling again** — which is the
orchestrator's job, along with the queue and the budget.

## Run the tests

```bash
make test       # go test ./...
make coverage   # coverage, enforced against the floor in the Makefile
```

The conformance suite walks up to the repo root to find the corpus, so it runs from
anywhere inside the checkout.
