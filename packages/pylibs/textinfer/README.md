# textinfer

> What a TEI-compatible text worker needs beyond [`worker`](../worker): TEI's error contract,
> the admission rule, and tokenised text through an ONNX graph in token-budgeted batches.

**Status: v0.** Extracted when `services/inferences-reranker` became the second caller after
`services/inferences-embedding`: two services needing the same code is the signal it belongs
here rather than in either of them.

| module | what it owns |
| --- | --- |
| `batching.py` | `open_session` (CPU, arena off), `plan_batches`, `pad`, `feeds` |
| `tei.py` | `TeiError` and TeiError's status per type, `Admission`, `flag`, `truncation_direction` |
| `errors.py` | `InvalidRequest` and `InputTooLong`: the failures that are the caller's |

**The batching rule.** Inputs are sorted longest first and grouped so that no group costs
more than `max_batch_tokens` once padded. That ceiling is what bounds peak memory, because
attention grows with the square of the padded length; splitting by count would not.

**The admission rule.** A route parses before it takes a slot, refuses past its limit with
429 `Overloaded` rather than queueing, and reaches the model only through
`ModelManager.run`, so a request never loads one. `Admission.run` turns every failure into
TEI's type: nothing resident is 503 `Unhealthy`, `InvalidRequest` is 422 `Validation`,
anything else is 424 `Backend`.

**The session.** ONNX Runtime's CPU memory arena is off: it keeps every high-water mark, and
on an embedding model one long batch left 3.7 GB pinned for good.

Status codes and field idioms are TEI's, taken from its router source
(`router/src/http/types.rs`, `server.rs`), not from memory.

## Dependencies

`worker` (a workspace source), numpy, and onnxruntime — the last imported only inside
`open_session`. `make py-verify` scans this package with those two allowed by name.

## Tests

```bash
make test
make coverage
```

Offline, no weights, no graph: tables for batching, feeds and the error contract, a scenario
for the admission limit, and `open_session` against a patched `InferenceSession`, which is
the seam: what it is handed is the whole of what this package decides about a session.
