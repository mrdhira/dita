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

- `criteria` is what the model reads as the question's instructions. Without it the model sees only the
  question's name with underscores as spaces. The orchestrator's `decisions.Question` carries it
  (`omitempty`), so a template's criteria reach the worker unchanged.
- A `noul` question's options are exactly `false` and `true`, in either order. The model answers a noul in no
  other terms, so the worker refuses anything else (400) and `ValidateDraft` refuses such a template
  (`specs/decisions/schema-cases.json` holds both sides to it).
- `range` is accepted on a `score` question and ignored: the model reads a score's options as its levels, in
  order (`level 0: <first option>`, …), and never sees the range. Dita's template schema defines it and the
  dashboard's editor emits it, so its presence never fails a request. It is refused (400) only when it
  contradicts its own options: on a question that is not a score, when it is not two numbers with min below
  max, or when numeric options are out of order or outside min..max. Options that are not numbers cannot
  contradict a range.

Reply — deliberately the shape `services/dita-orchestrator/decisions/worker.go` already documents and parses
(`workerReply` / `workerAnswer`). That file is the placeholder the orchestrator wrote *for this worker*, so
implementing its documented shape keeps the adapter's change to the additive `act_probability` field and
leaves its fixtures and the dashboard's contract untouched.

```json
{"model_id": "laya-multilingual",
 "model_revision": "<the HF revision in models.yaml>",
 "answers": [{"name": "severity",
              "probabilities": {"info": 0.0112, "warning": 0.8507, "critical": 0.1381},
              "confidence": 0.5801,
              "act_probability": 1.0}]}
```

Rules the adapter and the dashboard may rely on:

- Every option of the question appears as a key. The worker guarantees the values sum to 1 within 1e-12 (the
  final softmax is in float64); `ParseReply` does not check the sum, so nothing downstream should treat it as
  enforced by the adapter. The orchestrator orders the options best first itself (`Answer.Options`).
- `type: "noul"` (the model's boolean question) answers with the keys `false` and `true`.
- `confidence` is the model's own measure, upstream's `confidence_from_probs`: 1 minus the entropy of the
  answer distribution (after temperature) divided by log(k), for k options. It is 1 for a certain answer and 0
  for a uniform one, and it is not the top probability: the example's 0.5801 sits beside a top of 0.8507.
  Upstream emits none for a noul; this worker computes the same function with k = 2, so the field is never
  absent.
- `act_probability` is the model's own escalate head (index 0 of its two-way softmax), reported and never
  used to override an answer in v1. On `laya-multilingual` its logits sit near ±1500, so it is 1.0 for every
  input measured: a constant that looks like information and is not. Nothing may be built on it until a
  checkpoint whose act head varies lands (worker README, "Known behaviour of this checkpoint").
- An answer that does not cover every asked question is an error, not a partial reply. The orchestrator
  refuses to store a reply it cannot match (`ParseReply`).
- `GET /info` reports `model_id`, `model_revision`, `engine` and the bounds below; `GET /health` is 503 until a
  model is resident; `GET /metrics` is Prometheus, on the same port as the routes.

Bounds. One request is inside the model at a time; a second is refused with 429 at once, because behind the
model's single lock a second slot would only be a queue. A request whose planned work cannot finish in time is
refused with 413 before the encoder runs: the planned work is the padded tokens of its encoder batches
(questions grouped two at a time at `max_len`), and the limit is 8192. The derivation: 20 questions at 1024
tokens (20480 padded tokens) took 169.5 s on this host at load average 13.8, 8.3 ms a token; 8192 tokens is
about 68 s at that rate, inside a 100 s deadline that is itself inside the orchestrator's 120 s
`INFERENCES_TIMEOUT`. The deadline is checked before every encoder batch; a request that passes it stops there
and is answered 504 with how many batches were done, and never with a partial reply. Twenty questions at the
limit are 20480 tokens, so a template's size and its text's length together decide whether it is served.

The temperatures are the ones upstream reads: `rl_agent_config.json`'s `temperature` and
`temperature_by_options`. The checkpoint also carries a `temperature` buffer that upstream ignores; the worker
refuses to load a checkpoint whose buffer and config disagree, because then nobody can say which calibration is
intended.

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
  kept in the payload only. On this checkpoint it is a constant 1.0, so not yet.
- `criteria` means two things. In this contract it is the question's instructions; in upstream it is the
  per-option descriptions (choice `{option: description}`, noul `{true, false}`), which the model supports and
  this contract cannot pass. Proposed: rename the contract's field to `instructions` and add optional option
  descriptions. Not done here, because the rename moves stored templates, the template editor and the
  dashboard's schema.
