# Technical Design — services/inferences-system-one

> System One: unstructured text in, a typed decision out, with the model's own probabilities.
> Temporary model: `convaiinnovations/laya-multilingual`. The runtime question was settled by
> measurement in `[0]spike-runtime-decision.md` — this document is the service that follows from it.

## Context

The consumer half already exists on `main`, built before the worker:

- `handler/inferences/gateway.go` proxies `POST /api/inferences/decide` to
  `http://inferences-system-one:8080/decide`, and answers `503 not_running` while no worker exists.
- `handler/decisions/handler.go` stores what the worker answers and serves the dashboard.
- `inferences-dashboard` renders Decide, Decision, Eval, History and Templates pages over that API.
- `decisions/worker.go` says of itself: *"The worker's answer shape is not settled … the shape below is
  the documented stub contract; when inferences-system-one lands, this adapter changes and nothing else
  does."*

So this service **defines** the answer shape, and the same PR changes that adapter to match. Nothing else
in the orchestrator moves.

## Goals

- A real decision for a real input, end to end: text → worker → orchestrator → dashboard.
- The repo's rule holds: `onnxruntime`, `tokenizers` and `numpy` at runtime, **no torch**. The ONNX encoder
  is produced in the image's build stage, where torch is allowed to exist (decision of 2026-09-25; publishing
  the artifact is deferred tech debt, recorded in the spike document).
- DIP lifecycle like every other worker, and `infer` refused over DIP exactly as `inferences-embedding` and
  `inferences-reranker` refuse it — the data plane is this worker's own HTTP surface.

## Non-goals

- Fitting calibration temperatures on our own alerts: there are no labels yet.
- Publishing the ONNX artifact to HuggingFace (tech debt).
- Cutting the alert pipeline over to this worker (Dita instruments first; shadow mode after).
- Any change to the dashboard's pages beyond proving them against real answers.

## The contract (defined here, adopted by the orchestrator in this PR)

`POST /decide`

```json
{"text": "…any text…",
 "questions": [{"name": "severity", "type": "choice",
                "options": ["info", "warning", "critical"],
                "criteria": "…optional instructions…"}]}
```

Reply

```json
{"model_id": "laya-multilingual",
 "model_revision": "<the HF revision in models.yaml>",
 "answers": [{"name": "severity", "type": "choice",
              "options": [{"option": "warning", "probability": 0.8507},
                          {"option": "critical", "probability": 0.1381},
                          {"option": "info", "probability": 0.0112}],
              "confidence": 0.5801,
              "act_probability": 1.0}]}
```

Rules the adapter and the dashboard may rely on:

- Every option of the question appears, `probability` sums to 1 within 1e-6, ordered best first.
- `type: "noul"` (the model's boolean question) answers as options `false` and `true`.
- `confidence` is the top probability after temperature; `act_probability` is the model's own escalate head
  (index 0 of its two-way softmax) — reported, never used to override an answer in v1.
- An answer that does not cover every asked question is an error, not a partial reply. The orchestrator
  refuses to store a reply it cannot match (`ParseReply`).
- `GET /info` reports `model_id`, `model_revision`, `engine`; `GET /health` is 503 until a model is resident;
  `GET /metrics` is Prometheus, on the same port as the routes.

## Design

- Layout under `services/inferences-system-one/`: `system_one_worker/` (service code), `models.yaml`,
  `compose.yaml`, `Dockerfile`, `Makefile`, `tests/`, `.coveragerc`.
- Lifecycle through `packages/pylibs/worker`: handshake and the op set, `load`/`unload`, `livez`/`readyz`/
  `startupz`, socket at `SOCKET_PATH=/run/dita/inferences-system-one.sock`.
- Engine `onnx_decision`: an ONNX encoder run by onnxruntime, the decision head in numpy. The spike's
  `ort_agent.py` is a working prototype of exactly this — it reproduces `build_sequence`, the per-question
  marker gather, the temperature buckets and the `system_one` response shape. Port it, do not rewrite it.
  Adapted from the spike, the head's last transformer layer need only be computed for the rows that are read
  afterwards (CLS and the option markers): exact, and about half the head's cost at 1024 tokens.
- Tokenisation with `tokenizers` against the checkpoint's `tokenizer/tokenizer.json`. Budgets come from the
  checkpoint, not the brief: `max_len` 1024, `head_max_len` 256.
- Build, multi-stage: the builder stage has torch, onnxscript and the fp16 checkpoint, and runs the export
  (dynamo exporter, opset 18, fp32) to `/models/onnx/encoder.onnx` plus its `.onnx.data`; the runtime stage
  installs only `numpy`, `onnxruntime`, `tokenizers`. `models.yaml` pins the *inputs* — the upstream repo, an
  immutable revision, `sha256` and `bytes` for `model.safetensors` and `tokenizer/tokenizer.json` — and the
  fetcher refuses anything else. After the tech-debt item lands, it pins the published ONNX instead and the
  export step is deleted.
- Network: `expose: 8080`, **no host ports**, container `inferences-system-one` on `proxy`. The orchestrator
  already addresses it by that name; host-side consumers reach it through the orchestrator's REST.
- Memory: the spike measured a 1.7 GB peak. Set the compose cap from a measured peak for this service, not by
  guesswork; the spike's number is the starting point.

## Verification

- **Parity guard, in the test suite, without torch**: the spike's reference answers (`ref_out.json`) become
  fixtures; the test asserts the torch-free path reproduces them within 1e-4 with the argmax unchanged, and
  asserts that `torch` and `transformers` are not in `sys.modules`.
- **Blackbox**: a real socket and a real HTTP port, in the shape of
  `services/inferences-embedding/tests/test_service.py`.
- **End to end**: `POST /api/inferences/decide` through the orchestrator returns a real answer for a real
  alert text, and the dashboard's Decide page renders those probabilities.
- **The repo's gates**: `make doctor`, `make test`, `make coverage` (floor held), `make build`, `make dip-verify`.

## Open questions

- Calibration: the checkpoint ships every temperature at 1.0, so the temperature path is the identity until we
  fit per-bucket values on our own data.
- Quality: on a real alert both runtimes call an OOM crash loop a `warning` that needs no human. Until labels
  exist, this worker is a *shadow* predictor, not a gate.
- Whether `act_probability` should be stored and shown as the dashboard's "spawn AI or a human" signal, or
  kept in the payload only.
