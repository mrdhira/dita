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
- [ ] A4. `packages/` skeleton, language-first: `golibs/` and `pylibs/`, both toolchains wired.
- [ ] A5. Exact version pins: Python 3.14.6 everywhere, Go `toolchain` directives, empty
      `go.sum`/`go.work.sum`, `make doctor` asserting both, Dependabot docker + gomod entries.

## Order B — the DIP protocol + codegen

- [ ] B1. Specify the protocol in one place, language-neutral.
- [ ] B2. Generate the Python and Go bindings from it.
- [ ] B3. Both implementations point at the spec; neither is the source of truth.

## Order C — worker kit refactor + metrics

- [ ] C1. Extract the reusable worker parts into `packages/pylibs/`.
- [ ] C2. `services/inferences-ocr` becomes a thin engine layer over the kit.
- [ ] C3. The worker serves `/metrics` on its own small HTTP port.

## Verification — every order

- [ ] `make doctor` passes.
- [ ] `make test` and `make coverage` pass, per service, floor enforced.
- [ ] A root-context image build succeeds and the container reaches healthy.
- [ ] The Go reference client still completes `handshake → list → load → infer → unload`
      against a live worker.

## Review section

_(filled in at the end of each order: what changed, evidence, anything flagged)_

### Order A

_(pending)_
