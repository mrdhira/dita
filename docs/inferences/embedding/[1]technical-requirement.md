---
type: technical-design
status: in-review
owner: Dhira Wigata
product: dita
prd: n/a — internal infrastructure
date: 2026-09-23
tags:
  - dita
  - inferences
  - embedding
  - technical-design
---

# Technical Design — services/inferences-embedding

> House style that applies here: **stdlib-first, minimal dependencies**, and the shape
> `services/inferences-ocr` set. This worker is composition over
> [`packages/pylibs/worker`](../../../packages/pylibs/worker) and
> [`packages/pylibs/dip`](../../../packages/pylibs/dip); it owns an engine adapter, a
> manifest and three HTTP routes. One departure to justify: its workload is served over HTTP,
> because the consumer's contract is TEI. See Alternatives.

## Context

An external memory service (Hindsight, on this box's `proxy` network) needs text
embeddings. Its slim image carries no embedding model and exits at startup when no embedding
endpoint answers, so the model has to be served from somewhere we control. Hindsight speaks
HuggingFace Text Embeddings Inference (TEI): `GET /info` once, then `POST /embed`.

What forced the design:

- **The consumer's protocol is fixed.** TEI's `/embed` is the integration contract; DIP is
  not an option for a client we do not own.
- **The lifecycle is not the consumer's.** Which model is resident is the orchestrator's
  decision, made over DIP, exactly as for OCR. A request must never load a model.
- **The box is 16 GB and CPU-only**, shared with everything else on `proxy`. Measured, not
  estimated: see Cost.
- **The repository is public.** No weights, no tokens. One of the three models is gated on
  Hugging Face, and the fetcher has no auth by design.

## Goals and non-goals

**Goals**

- Three embedding models selectable, at most one resident, loaded and unloaded by the
  orchestrator over DIP.
- `POST /embed` taking `{"inputs": [...]}` and returning one vector per input, in order,
  compatible with TEI: field names, error bodies, status codes.
- Normalised output and a documented pooling strategy per model, reproducing each model
  card's recipe to within float32 noise.
- Query/document prefixes per model, configurable in `models.yaml`.
- Deployable as its own stack, reachable by container name, no host ports.

**Non-goals**

- A reranker. The intended follow-up is `Qwen/Qwen3-Reranker-0.6B`, which mirrors the
  embedding family's sizes (0.6B / 4B / 8B) and would be a second engine in this worker or
  a sibling worker; either way a separate change.
- An LLM of any kind.
- Changing the DIP spec. DIP carries the lifecycle only (see Decisions).

## Design

```
  dita-orchestrator (Go)            inferences-embedding (Python)
  ┌──────────────────┐              ┌────────────────────────────────────────┐
  │ model policy     │  DIP, AF_UNIX│ SocketServer ──► dispatch ─┐            │
  │ load / unload    │─────────────►│  load · unload · list      │            │
  └──────────────────┘ /run/dita    │                            ▼            │
                                    │                      ModelManager       │
  Hindsight (memory)                │                      one lock, one model│
  ┌──────────────────┐  HTTP :8080  │ metrics port ─► /embed ────┘  │         │
  │ TEI client       │─────────────►│   /info /health /metrics      ▼         │
  └──────────────────┘ proxy network│                    OnnxEmbedder (x1)    │
                                    │                    tokenizer + ORT graph│
                                    └────────────────────────────────────────┘
```

Two doors onto one `ModelManager`. The orchestrator's DIP socket decides what is resident;
the HTTP port asks the resident model for vectors through `ModelManager.run`, under the same
lock and the same metrics as a DIP `infer`. With nothing resident, `/embed` answers 503.

The HTTP port is the one the worker package already serves `/metrics` on, extended with
service-supplied routes (`Worker.routes`). It is not a second server.

### Data model

`models.yaml`, the only source of truth for what is selectable. The framework's fields
(`id`, `engine`, `langs`, `source`, `files`) are as for OCR. What this service adds is
`options`, read by `EmbedderConfig.from_options`:

| option | meaning |
| --- | --- |
| `onnx`, `tokenizer` | paths under the model directory; both must be pinned files |
| `pad_token` | the token id used for padding; masked out of every pooling |
| `output` | the graph output to read: `last_hidden_state`, or `sentence_embedding` when the graph pools |
| `pooling` | `mean`, `last_token`, or `graph` (the graph already pooled and normalised) |
| `dimensions` | the native output width |
| `matryoshka_dimensions` | the widths the model card says were trained; any `1..dimensions` is accepted, as TEI does |
| `matryoshka_layer_norm` | layer-norm the pooled vector before truncating (nomic's recipe) |
| `max_sequence_length` | the model card's context length, recorded for the reader |
| `max_input_tokens` | what this service accepts; measured, below the card where memory demands |
| `max_batch_tokens` | the padded-token budget one graph run may cost |
| `prompts` | name → prefix; TEI's `prompt_name` selects one |

### The three models

Every figure below was read from the model card and config files at the pinned revision, then
confirmed against the ONNX graph's own input and output shapes.

| id | upstream | weights served | dims | Matryoshka | card context | served | pooling | languages |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `nomic-embed-text-v1.5` | `nomic-ai/nomic-embed-text-v1.5` | its own `onnx/model.onnx`, fp32, 547 MB | 768 | 512, 256, 128, 64 | 8192 | 2048 | mean, then layer-norm before a Matryoshka cut | **English only** |
| `embeddinggemma-300m` | `google/embeddinggemma-300m` | `onnx-community/embeddinggemma-300m-ONNX`, fp32, 1.23 GB | 768 | 512, 256, 128 | 2048 | 2048 | in the graph: mean, Dense 768→3072, Dense 3072→768, L2 | 100+ |
| `qwen3-embedding-0.6b` | `Qwen/Qwen3-Embedding-0.6B` | `onnx-community/Qwen3-Embedding-0.6B-ONNX`, fp32, 2.4 GB | 1024 | any 32–1024 | 32768 | 2048 | last token (the appended `<\|endoftext\|>`) | 100+, strongest on Indonesian and Japanese |

**`nomic-embed-text-v1.5` is English-only and must not be selected for Indonesian or
Japanese text.** It is the default because the default language is English, by decision.
For Indonesian or Japanese, load `qwen3-embedding-0.6b`; `embeddinggemma-300m` is the lighter
multilingual option.

Prefixes, exactly as the cards give them:

| id | `query` | `document` | others |
| --- | --- | --- | --- |
| `nomic-embed-text-v1.5` | `search_query: ` | `search_document: ` | `clustering: `, `classification: ` |
| `embeddinggemma-300m` | `task: search result \| query: ` | `title: none \| text: ` | six more `task: … \| query: ` forms |
| `qwen3-embedding-0.6b` | `Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery:` | *(none)* | — |

nomic is asymmetric and degrades badly without its prefixes, which is why they are data
rather than a caller's afterthought. The prefix is prepended to the text before
tokenisation, so it is tokenised with it, as the reference implementations do.

Qwen's served limit is 2048, not its card's 32768: a single 8000-token input drove the
process past 12 GB and the host's OOM killer took it. nomic's is 2048 because it was trained
at 2048 (`max_trained_positions`) and its config sets no rotary scaling; longer inputs are
refused rather than served untested.

### Interfaces

**DIP**, unchanged: `handshake`, `list`, `load`, `unload`, the three probes. `infer` is
refused with `bad_request` and a message pointing at `/embed`.

**HTTP**, on `METRICS_ADDR` (`0.0.0.0:8080` in `compose.yaml`, loopback by default):

| route | TEI | this service |
| --- | --- | --- |
| `POST /embed` | `inputs` (string or list), `normalize` (true), `truncate` (false), `truncation_direction` (`right`), `prompt_name`, `dimensions` | the same fields and defaults; token-id inputs are not supported; an unknown field is refused |
| response | `[[f32, …], …]` | the same, each float at its shortest float32 spelling; `x-model-id` names the model that answered |
| `GET /info` | `model_id`, `max_input_length`, `max_client_batch_size`, … | the same fields, plus `dimensions` and `prompt_names` |
| `GET /health` | 200, or 503 `Unhealthy` | 200 while a model is resident |
| `GET /metrics` | — | the worker's Prometheus text |

Errors are TEI's body, `{"error": "...", "error_type": "..."}`, with TEI's status per type,
taken from its router source rather than from memory:

| `error_type` | status | when |
| --- | --- | --- |
| `Empty` | 400 | `inputs` is an empty list |
| `Validation` | 422 | unknown field, bad type, more than 32 inputs, an input past `max_input_tokens` without `truncate`, an unknown `prompt_name`, `dimensions` past the model |
| `Overloaded` | 429 | four requests already in flight; carries `Retry-After: 1` |
| `Backend` | 424 | the engine raised, or produced a non-finite value |
| `Unhealthy` | 503 | nothing resident |

The framework itself refuses a body with no `Content-Length` (411) or one over 2 MiB (413),
on the header, before reading it.

### Control flow

1. The orchestrator (today: `PRELOAD_MODEL` in `compose.yaml`) sends `load`. The fetcher
   verifies every file's sha256; the manager releases the old engine, then builds the new
   one: an ONNX Runtime session with its memory arena off, and the tokenizer.
2. A `/embed` request is parsed and validated without touching the lock. A slot is taken
   (four at most) or the answer is 429.
3. Under `ModelManager.run`: prompts prepended, tokenised without truncation; an input past
   the limit is refused, or re-tokenised with truncation so its special tokens survive.
4. Inputs are sorted longest-first and grouped so that no group costs more than
   `max_batch_tokens` once padded. Each group is padded on the right and run.
5. Pooled per `pooling`, scattered back into input order, then `finish`: layer-norm if the
   model's recipe asks for it and the vector is being cut, truncate, L2-normalise.
6. Serialised, returned with `x-model-id`.

## Alternatives considered

**A second HTTP server in the service.** Rejected: the worker already serves HTTP for
metrics, with the connection cap, timeouts and header hygiene that took several reviews to
get right. Routes on that port reuse all of it. The cost is that routes share its eight
connections with scrapes, which is why `/embed` caps itself at four.

**Carrying vectors in DIP `infer`.** Rejected for this change: `InferResponse` is closed to
`text` and `lines`, and the order forbids a spec change. The orchestrator does not need
vectors today. The generalised response shape is already an open question from OCR's design.

**sentence-transformers or transformers at runtime.** Rejected: torch. `tokenizers` reads
the same `tokenizer.json` and ONNX Runtime runs the same graph; the reference comparison
below shows the result is the same to 1e-6.

**Qwen3 int8.** Measured and rejected: 2.1× faster, but its vectors are only 0.48–0.87
cosine from fp32 and it changed a cross-lingual nearest neighbour. That is a different
model.

**`google/embeddinggemma-300m` directly.** Not possible without a Hugging Face token: the
repository is gated. The `onnx-community` export is ungated and reproduces the card's
published similarities to 1.8e-7. The Gemma Terms of Use still apply to it.

## Migration and rollout

Nothing migrates: this is a new worker. For the memory service, the cutover is the env block
in the service [README](../../../services/inferences-embedding/README.md#the-memory-service),
followed by switching Hindsight back to its slim image.

**Changing the resident model changes the vectors, and possibly their width.** A memory
store holds vectors from one model; its index is built for one dimension. Swapping nomic for
Qwen under a running Hindsight means re-embedding everything it holds. The orchestrator must
treat the model behind a consumer's store as fixed, and `x-model-id` is there so a client can
check.

## Rollback

`make down` in the service directory. Hindsight goes back to its full image, which embeds
in-process. No state here needs undoing: the models directory is a cache.

## Observability

The worker's metrics, unchanged: `dita_worker_infer_duration_seconds` counts every
successful `/embed` under the model that answered, and the resident gauges name the model.
A refused request is not counted as an inference.

## Security and privacy

- No host ports. The port is on `proxy` only; Caddy fronts anything public.
- No token anywhere. The gated upstream is served from an ungated export instead.
- A body is capped at 2 MiB and refused on its declared length before it is read.
- Inputs are memory contents. They are not logged; only failures are, with the exception
  type and message.

## Testing

```bash
make test        # 43 tests, offline, no weights
make coverage    # 95%, floor 93
```

Blackbox first: `LifecycleTest` loads and unloads over a real DIP socket and watches `/embed`
over real HTTP answer 503, 200, 503. `EmbedEndpointTest` drives every refusal and option
through HTTP against a one-slot concurrency cap, so a failure path that leaks its slot turns
the next row into a 429. Behind them is the real `Embedder`, a real `tokenizers` tokenizer
built in memory, and a fake session whose hidden state encodes the token ids it was given.

Tables for the pure parts: pooling (mean, last token with left and right padding, graph),
Matryoshka order, batch planning, config refusals, TEI parsing, TEI's status per error type.

**Mutation.** Twenty deliberate breakages of the logic, each caught by the test that should
catch it; two survived the first pass and exposed weak tests, which were fixed. Eleven more
on the framework seam in `packages/pylibs/worker`.

**Against the reference implementations**, with the real weights, outside the suite:

| model | reference | query | document | 256-d Matryoshka |
| --- | --- | --- | --- | --- |
| nomic-embed-text-v1.5 | sentence-transformers 3.4.1, transformers 4.48.3, fp32 | max abs diff 1.7e-7 | 2.1e-7 | 2.7e-7 (the card's layer-norm recipe) |
| qwen3-embedding-0.6b | sentence-transformers 5, fp32 | 6.6e-7 | 4.8e-7 | 9.7e-7 |
| embeddinggemma-300m | the card's published similarities | 1.8e-7 | — | — |

The first Qwen comparison showed 2e-3 differences; the reference had loaded in bfloat16, the
config's dtype. Forced to fp32 it agrees to 1e-6.

Not covered by the suite: the ONNX Runtime constructor, which needs weights; model quality.

## Cost

6-core CPU, `EMBEDDING_THREADS=4`, fp32, after load. Latency is the whole request.

| model | load (warm cache) | 32 × ~100 tok | 32 × ~400 tok | 2 × ~2000 tok | peak RSS |
| --- | --- | --- | --- | --- | --- |
| nomic-embed-text-v1.5 | 10–17 s | 3.0 s | 13–17 s | 12–14 s | 2.2 GB |
| embeddinggemma-300m | 10–21 s | 3.3 s | 15.5 s | 10.2 s | 1.3 GB |
| qwen3-embedding-0.6b | 25–67 s | 14–21 s | 55–60 s | 28 s | 5.2 GB |

`mem_limit` is 6g because a DIP `load` can make any of them resident: Qwen3 peaked at
5197 MiB inside the capped container, with no OOM kill. With the ONNX Runtime arena on,
nomic alone held 3.7 GB after one long batch and never gave it back; with it off, the peak
is set by `max_batch_tokens` and the resident size falls back afterwards.

Qwen3 is slow here, and Hindsight's TEI client reads with a 30-second timeout: 32 passages
of 400 tokens take a minute. Eight take 15.0–15.8 s on Qwen3 and 3.5–3.6 s on nomic, measured
in the deployed container; hence the smaller batch in the Hindsight settings.

## Decisions

**The TEI surface is on the metrics port.** Routes are a framework feature
(`Worker.routes`), not a service-local server. Any worker whose consumer is HTTP by contract
uses the same seam.

**`/embed` never loads.** With nothing resident it answers 503 `Unhealthy`, which
Hindsight retries. Lifetime is the orchestrator's; `PRELOAD_MODEL` is how the deployment
stands in for it today, and a DIP `load` replaces the preloaded model.

**Prefixes are opt-in per request.** With no `prompt_name` nothing is prepended, as in TEI.
Hindsight prepends its own configured prefixes client-side, so the service adding one too
would double it.

## Open questions

- [ ] **Q:** DIP `InferResponse` cannot carry vectors. Does the orchestrator ever need them
  over DIP, or is HTTP the embedding surface for good? Shares its answer with OCR's open
  question on a generalised response. — *owner:* Dhira
- [ ] **Q:** Tokenisation runs under the manager lock. At four concurrent requests it is
  small next to the graph, but it serialises for no reason. Worth moving out once measured.
- [ ] **Q:** Qwen3 on this CPU is slow enough to trip a 30-second client timeout at TEI's
  default batch. Quantise a better export, or accept it as the multilingual price?
- [x] **Follow-up:** `Qwen/Qwen3-Reranker-0.6B` for `/rerank` (0.6B / 4B / 8B, like the
  embedding family) is [`inferences-reranker`](../reranker/%5B1%5Dtechnical-requirement.md),
  a separate worker. Hindsight's reranker points there, never here.
