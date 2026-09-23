---
type: note
status: open
owner: Dhira Wigata
product: dita
date: 2026-09-12
tags:
  - dita
  - protocol
  - dip
---

# Two decisions for Dhira

Neither is a fix, so neither was made. Both came out of the independent review of orders B
and C.

## 1. The STT seam stops at the response shape

**What holds.** `packages/pylibs/worker` genuinely does not know what an engine is. It
takes a factory and a `Worker`, and a test builds a complete working worker from a six-line
fake engine while importing nothing from any service. Socket, framing, residency, digests,
health, CLI and metrics are all reusable as claimed.

**What does not.** The result type is OCR's. `engines.py` defines `Result(text, lines)` and
`Line(text, confidence, box)` with a four-corner box, and `InferResponse` in the IDL carries
`additionalProperties: false`, so the shape is closed at the protocol level too.

A speech worker can technically return a transcript today: one `Line`, `box: null`. That is
the workaround the package README shows. What it cannot do is carry the things a speech
result is actually for — segment boundaries, per-segment timestamps, speaker labels,
alternatives with their own confidences.

**What generalising it would cost.** Three options, in ascending order of disruption:

| option | cost | what it buys |
| --- | --- | --- |
| **Add optional fields to `Line`** — `start_s`, `end_s`, `speaker` | Schema edit, regenerate both languages, new `responses.json` cases. No version bump: the spec now says adding an optional response field does not force one, and the response types no longer close themselves. | Speech fits, awkwardly. `box` and `start_s` on one type means every consumer reads fields that are null for half of all workers. |
| **A per-modality result variant** — `InferResponse.lines` becomes a union of `TextLine` and `AudioSegment`, discriminated | Schema work, generated types grow a union both languages must switch on, every reader updated. One version bump. | Honest shapes per modality. The cost lands on every consumer, including the orchestrator. |
| **An opaque `result` object** — the worker returns engine-shaped JSON the protocol does not interpret, with `text` kept as the common denominator | Largest protocol change, smallest ongoing cost. Removes the generated types' value for the result body specifically. | Any future modality fits without touching DIP. Gives up compile-time shape checking exactly where the payload varies most. |

**A recommendation, since one was not asked for but is cheap to give.** The middle option is
the one that ages best, but not yet: it costs a version bump and a change to every consumer,
and there is currently one consumer and no speech worker. The first option is enough to
build `inferences-stt` and learn what the shape actually needs to be, and it costs no bump.
Widening on evidence beats widening on a guess.

**What this does not block.** Everything else about `inferences-stt` — the socket, the
manifest, the residency invariant, health, metrics — works today.

## 2. Nothing runs the gates

`.github/` contains `dependabot.yml` and no workflows directory. `make test`, `make coverage`
and `make dip-verify` run only when someone runs them. `make test` now depends on
`dip-verify`, so the zero-dependency bar is at least reachable from a normal command, but
nothing enforces any of it on a push or a pull request.

**What a minimal gate would need**, stated rather than added, per the order:

- **One workflow, on `push` and `pull_request`.** Ubuntu runner; the repo is Linux-only in
  practice and macOS support in `doctor` is untested anyway.
- **Toolchain from the pins.** `astral-sh/setup-uv` for uv 0.12.13 and `actions/setup-go`
  reading `go.work`'s directives. Python comes from uv itself, so `.python-version` is
  already the single source.
- **Four commands**: `make doctor`, `make test`, `make coverage`, `make build`. `doctor`
  first, because a green suite on a machine with the wrong toolchain proves less than it
  looks like. `build` last, because it is the slowest and the least likely to fail alone.
- **One caveat that decides the shape.** `make doctor` checks that the Docker daemon is
  reachable, which is true on a GitHub runner but would need `--skip docker` on a runner
  without it. Either add that flag or accept the dependency.
- **Caching**: the uv cache keyed on `uv.lock`, and the Go build cache keyed on the `go.sum`
  files. Without them the OCR image build dominates the run.
- **What it cannot check**: the live end-to-end run needs model weights (21 MB for PP-OCRv5,
  fetched at first load), so either the job downloads them each time or the cross-language
  live test stays a local check. The conformance corpora need neither and should run always.

Roughly forty lines of YAML. The decision to make is whether CI pulls model weights, because
that is what separates a two-minute job from a five-minute one.
