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
| `internal/conformance/` | Finds the shared corpus and decides what an absent one means. Test support, not protocol. |

One test file per source file, black-box (`package dip_test`) unless something unexported
forces otherwise:

| file | what it covers |
| --- | --- |
| `framing_test.go` | `DefaultLimits`, `Error`, `CodeOf`, `ValidateControl`, and the `dispatch.json` cases. |
| `framing_internal_test.go` | The framing itself, which is unexported: `writeMessage`, `readMessage`, the `datagramSource`/`datagramSink` interfaces the recorded datagrams need, and the `opFields` table `dispatch.json` cross-checks. |
| `requester_test.go` | The requester over a real `SOCK_SEQPACKET` socket, and `ResidentModel`. |
| `types_test.go` | The generated decoders, and the `responses.json` cases. |

`receiver.go` has no test file because it has no code — it is documentation of a role that
is deliberately unimplemented, and an empty test file would only imply otherwise.

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

The corpus is found by walking up from the working directory to `specs/dip/conformance`, so
the suite runs from anywhere inside the checkout. It lives outside this module on purpose —
it belongs to neither implementation — which means a checkout of this module alone may not
have it. Three rules, in `internal/conformance`:

- **`DIP_CONFORMANCE_CORPUS` names the directory outright, and turns off every excuse.** With
  it set, a wrong path, a missing file or malformed JSON is a failure and nothing skips. CI
  and any tree that ships the corpus should set it.
- **No corpus anywhere above is a skip**, with a message naming the file that did not run.
  The package's own tests still run and still assert the same rules — the corpus proves Go
  and Python agree, not that Go is right — so an absent corpus costs about half a point of
  coverage rather than the suite's meaning.
- **A corpus directory missing a file is fatal, never a skip.** A partial corpus is
  corruption, not absence.

`go test` without `-v` prints nothing at all for a package whose tests skip — not even output
written straight to stderr, which the tool buffers and discards for a package that passes. A
skip here is therefore invisible at the default verbosity, which is why `make test` and
`make coverage` print the corpus status before they run anything.
