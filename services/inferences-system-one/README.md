# inferences-system-one

> System One: unstructured text in, a typed decision out, with the model's own probabilities.

**Status: v0, in review, a shadow predictor.** Composed from
[`packages/pylibs/worker`](../../packages/pylibs/worker) like every inference worker. The runtime was
settled by measurement in the [spike](../../docs/inferences/system-one/%5B0%5Dspike-runtime-decision.md);
the service and its contract are the
[technical requirement](../../docs/inferences/system-one/%5B1%5Dtechnical-requirement.md).

## TL;DR

```bash
make -C services/inferences-system-one up      # its own stack, on the `proxy` network
```

- **One model:** `laya-multilingual` (`convaiinnovations/laya-multilingual`, Apache-2.0), 100+ languages
- **`POST /decide`** on `http://inferences-system-one:8080`, in the shape the orchestrator's
  `decisions/worker.go` parses; `GET /info`, `GET /health`, `GET /metrics` on the same port
- **ONNX Runtime, `tokenizers` and numpy only.** The encoder is an ONNX graph exported in the
  image's builder stage, the only place torch exists; the decision head is numpy
- **Parity is a build gate**: the image does not exist unless the torch-free path reproduces the
  torch reference within 1e-4 on six inputs
- **Uncalibrated.** Every temperature is 1.0; treat the probabilities as over-confident

## The contract

```bash
curl -s http://inferences-system-one:8080/decide -H 'Content-Type: application/json' -d '{
  "text": "alert: service immich stopped responding; container exited with code 137 (OOM)",
  "questions": [
    {"name": "severity", "type": "choice", "options": ["info", "warning", "critical"],
     "criteria": "How severe is this homelab alert?"},
    {"name": "needs_human", "type": "noul", "options": ["false", "true"]}]}'
```

```json
{"model_id": "laya-multilingual", "model_revision": "b4a904d1a2a54c822b829e24291d4b8f280fe43e",
 "answers": [{"name": "severity", "probabilities": {"info": 0.011, "warning": 0.851, "critical": 0.138},
              "confidence": 0.580, "act_probability": 1.0}, ...]}
```

- Every option is a key; the values sum to 1 within 1e-12. `confidence` is the model's own measure,
  upstream's `confidence_from_probs`: 1 minus the answer's entropy over log(k). It is 1 when certain, 0
  when uniform, and not the top probability. A noul gets the same measure with k = 2.
- **A `noul` question's options must be exactly `false` and `true`**: that is the only way the
  model answers one. A template whose noul says `yes`/`no` is refused with a 400, never guessed at.
- `criteria` is the model's instructions for that question. Without it the question's name is
  the only text that says what is asked, so it is used instead; write criteria.
- `range` on a score is accepted and ignored: the model reads a score's options as its levels, in
  order. It is refused only when it contradicts those options (numeric levels out of order or outside it).
- `act_probability` is the model's escalate head. On this checkpoint it is 1.0 for every input
  measured (its logits sit near ±1500), so it carries no signal yet.
- One request at a time. The worker's own refusals carry a JSON body `{"error", "error_type"}`:
  400 for a request it will not run; 413 when the planned encoder work (padded tokens, two
  questions a batch) is over 8192, which would not finish inside the deadline; 429 while another
  request is inside the model; 503 with nothing resident; 504 when the 100 s deadline passes
  between batches, with nothing returned; 424 when the engine fails. `/info` states the bounds.
- The framework's own refusals are not JSON: a path it does not route (404), a POST without a
  length (411), a body over 2 MB (413) and a route that raised (500) come from `http.server` as HTML.
- A question whose options cannot fit the head's 256-token budget would be refused by the engine,
  but with at most 20 options upstream's layout always shrinks them to fit, so the worker never
  answers that 422 under this checkpoint. Long options sharing a prefix can be cut to the same
  text; the model then cannot tell them apart and nothing says so.

## How to use it

### Deployed

`compose.yaml` runs `inferences-system-one` on the external `proxy` network with no published
ports and `PRELOAD_MODEL: laya-multilingual`. The checkpoint the head reads (680 MB) is fetched
into `models/` on first load and verified by sha256; the encoder graph (1.2 GB) is in the image.
The healthcheck is HTTP `/health`, which is 503 until a model is resident: a failed preload makes
the container unhealthy rather than a green one that answers 503 forever.

### From the orchestrator

DIP at `/run/dita/inferences-system-one.sock` for `load` / `unload`, as for every worker. `infer`
over DIP is refused: its response shape cannot carry probabilities.

### Locally

```bash
make export     # fetch the pinned inputs into ./models and export the encoder (torch, its own env)
make parity     # the parity guard against ./models, torch-free
make run        # HTTP on 127.0.0.1:8082, socket in ../../run
```

`SYSTEM_ONE_THREADS` sets ONNX Runtime's intra-op threads; `SYSTEM_ONE_ONNX_DIR` says where the
graph is (the image sets `/opt/system-one/encoder`; locally it is `models/laya-multilingual/onnx`).

## Contributions

```bash
make test       # offline, no weights; the parity guard skips and says so
make coverage   # floor 98
make parity     # needs `make export` first
make image      # runs the export and the parity guard inside the build
make golden     # regenerate tests/fixtures/golden_head.npz (torch)
make reference  # regenerate tests/fixtures/ref_out.json from upstream's own code (torch)
```

**`make test` does not guard the model.** It has no weights, so it cannot see the real head, the
encoder graph, or anything that only shows at the real width. What it does hold: the numpy head's
arithmetic against upstream's head in torch at a fake width of 128 with two heads
(`golden_head.npz`), the sequence layout, the contract and the service's refusals. The real model is
guarded by `make parity` and by the image build, which runs the same guard and fails on a skip.

`tests/test_service.py` is blackbox: a real DIP socket and a real HTTP port, with a real
`Decider` behind them on a fake graph. `tests/test_engine.py` holds the port's own logic to the
reference layout and to the golden head; `tests/test_parity.py` holds it to the reference's numbers,
through both upstream's request shape and the contract's.

The encoder graph and the head must come from the same bytes: the export stamps the sha256 it was
built from, and the engine refuses a graph whose stamp is not the `model.safetensors` digest in
`models.yaml`. Bumping the revision without rebuilding the image fails at load, not in the answers.
