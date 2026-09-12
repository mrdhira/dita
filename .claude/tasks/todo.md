# Task: shared packages and the DIP protocol

Branch `feat/packages-and-dip`, off `main` at b80e433 (PR #1 merged). One branch, one PR,
three work orders. Legend: `[ ]` todo · `[x]` done.

Previous plan archived at [`_archive/2026-09-12-inferences-ocr.md`](_archive/2026-09-12-inferences-ocr.md).

## Context

`services/inferences-ocr` proved the worker shape. Two more workers are coming, so the parts
both ends share have to move out of the OCR service before they get copied. The protocol
itself becomes a specified contract with generated bindings rather than two hand-written
implementations that agree today.

## Order A — foundation (this one)

- [x] A1. `.claude/agents/` — three subagents, current format, one tack each, parallel-safe.
- [x] A2. `.claude/rules/` — Python, Go and QA guidelines, concrete rather than aspirational.
- [x] A3. `.claude/tasks/` — archive the OCR plan, fresh todo, seed `lessons.md`.
- [x] A4. `packages/` skeleton, language-first: `golibs/` and `pylibs/`, both toolchains wired.
- [x] A5. Exact version pins: Python 3.14.6 everywhere, Go `toolchain` directives, empty
      `go.sum`/`go.work.sum`, `make doctor` asserting both, Dependabot docker + gomod entries.

## Order B — the DIP protocol + codegen

- [x] B1. Specify the protocol in one place, language-neutral: `specs/dip/dip.schema.json`
      plus `docs/protocol/[1]dip-specification.md`, with the alternatives that lost.
- [x] B2. Conformance corpus both languages read: 21 framing cases, 16 dispatch cases.
- [x] B3. Generate the Python and Go types from the IDL, zero third-party dependencies.
- [x] B4. `packages/pylibs/dip` — both roles; `services/inferences-ocr` imports it.
- [x] B5. `packages/golibs/dip` — requester implemented, receiver designed; the Go example
      uses it.
- [x] B6. Per-unit Makefiles and coverage for packages and examples, both languages.

## Order D — the independent review's findings

- [x] D1. The 12 must-fix items, each verified before fixing and mutated after.
- [x] D2. The smaller findings, each verified; two were already false on disk.
- [x] D3. Docs stop contradicting the code; the IDL and the spec agree on extensibility.
- [x] D4. The two decisions reported back rather than made:
      [`docs/protocol/[2]open-questions.md`](../../docs/protocol/%5B2%5Dopen-questions.md).

## Order C — worker kit refactor + metrics

- [x] C1. Extract the reusable worker parts into `packages/pylibs/`.
- [x] C2. `services/inferences-ocr` becomes a thin engine layer over the kit.
- [x] C3. The worker serves `/metrics` on its own small HTTP port.

## Verification — every order

- [ ] `make doctor` passes.
- [ ] `make test` and `make coverage` pass, per service, floor enforced.
- [ ] A root-context image build succeeds and the container reaches healthy.
- [ ] The Go reference client still completes `handshake → list → load → infer → unload`
      against a live worker.

## Review section

_(filled in at the end of each order: what changed, evidence, anything flagged)_

### Order C

Done. `packages/pylibs/dita-worker` holds everything a worker does except the inference;
`services/inferences-ocr` is three adapters, a manifest and a 36-line entrypoint. The seam
is proved by a test that builds a whole worker from a fake engine and drives it over a real
socket, importing nothing from any service.

Metrics are Prometheus text on the worker's own small HTTP port, loopback by default. Every
counter was checked live: after one session the numbers matched the work exactly, and a cold
load moved `fetched_bytes_total` to the byte count `models.yaml` pins. VictoriaMetrics is
opt-in behind a compose profile and scraped it successfully; port 9109 stays unreachable
from the host.

### Order B

Done. DIP specified, generated for both languages with zero third-party dependencies,
implemented as `packages/{golibs,pylibs}/dip`, and both the service and the example rewired
to it. Four units now, each with its own coverage number.

The live cross-language run is what earned its keep: every suite was green while the
generated Go validator refused every real inference response, because `go-jsonschema`
applied `box`'s outer `minItems` to its inner point arrays. A named `Point` fixes it, and
`responses.json` is the corpus layer that now guards it.

### Order A

Done. Six 0-byte files filled (three agents, three rules), the OCR plan archived, nine
lessons seeded, `packages/golibs` and `packages/pylibs` wired into both toolchains, and
Python 3.14.6 / Go 1.27.1 pinned exactly with `make doctor` asserting both.

One instruction could not be followed literally: a `toolchain` directive in every `go.mod`.
Go rejects one equal to the `go` directive — `go build` fails and `go mod tidy` deletes it —
and it broke the orchestrator image build. The `go` line at full patch is the pin instead,
and `go.work` keeps both directives. Reproduced minimally in both directions.
