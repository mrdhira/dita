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
  - reranker
  - technical-design
---

# Technical Design — services/inferences-reranker

> House style that applies here: **stdlib-first, minimal dependencies**, and the shape
> `services/inferences-embedding` set. This worker composes
> [`packages/pylibs/worker`](../../../packages/pylibs/worker),
> [`packages/pylibs/dip`](../../../packages/pylibs/dip) and
> [`packages/pylibs/textinfer`](../../../packages/pylibs/textinfer); it owns one engine
> adapter, a manifest and three HTTP routes.

## Context

The memory service, Hindsight, runs a `-slim` image with no models in it. It needs an
embeddings endpoint — [`inferences-embedding`](../embedding/%5B1%5Dtechnical-requirement.md)
— and a reranking endpoint. Without a reranker, recall rests on rank fusion, which never
reads a query and a memory together. A cross-encoder does, and it is the difference between
"found something related" and "found the right thing". Both endpoints are ours, on this box.

What forced the design:

- **The consumer's protocol is fixed**: Hindsight's TEI reranker client calls `GET /info`
  once, then `POST /rerank` with `{"query", "texts", "return_text": false}`, and reads
  `index` and `score` from each entry.
- **The lifecycle is the orchestrator's**, over DIP, exactly as for the sibling services.
- **The box is 16 GB and CPU-only**, with no GPU path. A 0.6B cross-encoder is the right
  size for it, and even so it is slow: see Cost.
- **The repository is public**: no weights, no tokens.

## Goals and non-goals

**Goals**

- `Qwen/Qwen3-Reranker-0.6B`, resident, loaded and unloaded by the orchestrator.
- TEI's `/rerank` contract exactly, verified against TEI's source, so Hindsight consumes it
  through `HINDSIGHT_API_RERANKER_TEI_URL` with nothing else changed.
- Pairs grouped by query and batched by a padded-token budget; a configurable maximum length.
- The same admission rule as the embedding service: never a silent wrong answer, never an
  implicit load.
- Its own stack on `proxy`, reachable by name, no host ports.

**Non-goals**

- Embeddings (the sibling service), an LLM, training, a GPU path.
- Changing the memory service's configuration. The cutover is documented, not applied.

## The TEI rerank contract, as verified

Source: `huggingface/text-embeddings-inference`, commit
`29ccc53ba56c9b4f4de8f19a14858d527fab680d` (2026-09-17), read in `router/src/http/types.rs`
(`RerankRequest`, `Rank`, `RerankResponse`), `router/src/http/server.rs` (`rerank`, route
`/rerank`, the status per `ErrorType`) and `core/src/infer.rs` (`predict`, the score).

**Request**, `POST /rerank`, JSON:

| field | type | default | meaning |
| --- | --- | --- | --- |
| `query` | string | required | |
| `texts` | array of strings | required | the candidates, scored against `query` |
| `truncate` | bool or null | `null` → the server's `auto_truncate` | cut an over-long pair instead of refusing it (`req.truncate.unwrap_or(info.auto_truncate)`) |
| `truncation_direction` | `left`/`Left`, `right`/`Right` | `right` | which end is cut |
| `raw_scores` | bool | `false` | return the logit rather than the score |
| `return_text` | bool | `false` | echo each text in its entry |

**Response**, 200: a JSON array with one entry per text, **sorted by score, descending**.

```json
[{"index": 2, "score": 0.99815416}, {"index": 1, "score": 0.96672606},
 {"index": 3, "score": 1.76402e-05}, {"index": 0, "score": 5.093705e-06}]
```

`index` is the text's position in the request; `text` appears only with `return_text`
(`skip_serializing_if = "Option::is_none"`). For a one-logit reranker, TEI's `score` is
`sigmoid(logit)`, and with `raw_scores` it is the logit itself.

**Errors**, `{"error": "...", "error_type": "..."}`: empty `texts` is 400 `Empty` ("`texts`
cannot be empty"); more than `max_client_batch_size` texts is 422 `Validation` ("batch size N
> maximum allowed batch size M"); a NaN score is 424 `Backend` ("score is NaN"); a model that
is not a reranker is 424. `/info` reports `model_type: {"reranker": {"id2label", "label2id"}}`.

**Where this service differs**: an unknown field is refused with 422 rather than ignored, as
in the embedding service; ties are broken by request order (TEI's reverse of a stable
ascending sort puts the later index first). Hindsight maps scores back by `index`, so
neither changes its behaviour.

## Design

```
  dita-orchestrator (Go)            inferences-reranker (Python)
  ┌──────────────────┐              ┌────────────────────────────────────────┐
  │ load / unload    │  DIP, AF_UNIX│ SocketServer ──► dispatch ─┐            │
  └──────────────────┘ /run/dita    │                            ▼            │
                                    │                      ModelManager       │
  Hindsight (memory)                │                      one lock, one model│
  ┌──────────────────┐  HTTP :8080  │ metrics port ─► /rerank ───┘  │         │
  │ TEI reranker     │─────────────►│   /info /health /metrics      ▼         │
  └──────────────────┘ proxy network│                  OnnxCrossEncoder (x1)  │
                                    └────────────────────────────────────────┘
```

The same two doors as the embedding service. `/rerank` reaches the model only through
`textinfer.Admission`, which is `ModelManager.run` with TEI's error types and a cap of four
requests inside at once; past that, 429 at once rather than a queue.

### Data model

`models.yaml` lists the models this service can serve; exactly one is active — `default_model`,
mirrored by `PRELOAD_MODEL` in `compose.yaml`, and both must name a model that is not commented out.
As of 2026-09-25 the active model is `jina-reranker-v1-turbo-en`. It replaced the fp16 Qwen3
reranker because the cross-encoder is **99.2% of a memory recall's latency** on this box (30.4 s of a
30.6 s recall, traced per phase), turbo-en scores ~10-40x cheaper per pair, and every memory in the
bank is English — the multilingual ability we were paying for was unused. Its graph is the official
export, so one repository suffices:

| file | from | why |
| --- | --- | --- |
| `onnx/model.onnx` (151 MB, fp32) | `jinaai/jina-reranker-v1-turbo-en` @ `b8c14f4e` | official ONNX export; fp32 because onnxruntime has no fp16 kernels without AVX-512 and int8 wants VNNI this CPU lacks |

`jina-reranker-v2-base-multilingual` is recorded in the same file as a **commented entry**: the
multilingual (en/id/ja) 12-layer model, 1.11 GB of weights against turbo-en's 147 MB. Switching is one
move (uncomment, fetch, point `default_model` and `PRELOAD_MODEL` at it) and is deferred until memory is
first retained in Indonesian or Japanese, or until turbo-en is measured to rank worse on our own data.
The model before that, `Qwen3-Reranker-0.6B` (fp16, 1.19 GB, `shawnw3i/…` graph plus the Qwen
tokenizer), was the highest-quality scorer measured at ~0.65-0.68 s per pair; it is history now.

Either way, **nothing about the stored memories changes**: the reranker only reorders the candidates
retrieval already found, so there is no re-embedding, no re-ingestion and no re-consolidation.

| option | meaning |
| --- | --- |
| `onnx`, `tokenizer`, `pad_token`, `output` | the graph, its tokenizer, the padding token, the output to read (`logits`, one per pair) |
| `body` | the pair as one template. Defaults to the Qwen card's prompt; a model with its own pair format sets it (jina reads a RoBERTa pair, `{query}</s></s>{text}`). Validated: `{query}` and `{text}` exactly once, nothing outside those three placeholders |
| `prefix`, `suffix`, `instruction` | the Qwen card's prompt, verbatim; optional, and empty for a model that sets its own `body` |
| `max_sequence_length` | the card's context, 32768, recorded for the reader |
| `max_input_tokens` | the most one pair may be, prompt included: **1024**, measured |
| `max_batch_tokens` | the padded-token budget of one graph run: **1024**, measured |
| `auto_truncate` | TEI's server flag: the `truncate` a request gets when it does not say |

### How a pair becomes a score

The active graph is a reranker converted to a one-logit sequence classifier, and that single logit is
the score: `sigmoid` of it is the model's published relevance. For the Qwen3 graph this service was
built around, the logit is the original model's `yes` logit minus its `no` logit, which is exactly
`softmax([no, yes])[yes]` and exactly TEI's single-logit score; jina's cross-encoders are trained with
that one logit already, so the reading is the same. Each pair is:

```
<prefix> + tokenize(<body>) + <suffix>
```

with the prefix and suffix tokenised without special tokens and the body with the tokenizer's defaults,
exactly as the model's reference code does. `<body>` is the model's own pair template: the Qwen string
by default, and for jina a RoBERTa pair, `{query}</s></s>{text}` — its tokenizer's post-processor wraps a
pair as `<s> A </s></s> B </s>`, so writing the separators into the string is what reproduces the
reference's own `tokenize(query, text)`. A test holds the chosen body's ids to the tokenizer's pair
encoding, so a body that does not reproduce the reference fails the build rather than quietly scoring
garbage (`make parity`).

For the Qwen graph the instruction is configuration, because TEI's request has no field for it: the
card's default is `Given a web search query, retrieve relevant passages that answer the query`. A model
that sets its own `body` needs no instruction, so its entry omits the three prompt fields entirely.

**Truncation cuts only the document.** The token where the document starts is found from the
encoding's offsets, and only tokens after it are dropped, from the end (`right`) or the start
(`left`). The instruction and the query always survive; a query that alone fills the limit
is refused. For `right`, the default, this is the reference's own truncation whenever the
query fits.

**`auto_truncate` is on.** Hindsight never sends `truncate`, and with it off, one memory
longer than 1024 tokens would turn its whole recall into a 422. Cutting a document's tail is
what the reference does anyway. A caller can still send `truncate: false` to be refused.

**Grouping and batching.** A request is one query group. Its pairs are sorted longest first
and cut into batches of at most `max_batch_tokens` padded tokens (`textinfer.plan_batches`),
right-padded; the graph finds each row's last real token itself. Scores go back to request
order before sorting.

### Admission when the model is not resident

The embedding service's rule, unchanged: `/rerank` with nothing resident answers **503
`Unhealthy`**, never a load and never a guess. `/info` and `/health` answer 503 too.
Hindsight retries 5xx with backoff, so a worker still preloading is a delay rather than a
failed recall. A request refused for its own reasons is 422; a failure of the model is 424.

## Verification against the official model

The reference is the card's own `transformers` code, run on the official weights
(`Qwen/Qwen3-Reranker-0.6B` @ `e61197ed`, `model.safetensors` sha256 `27cd75a4…`) in
**float32**, over twelve pairs: English, Indonesian and Japanese queries against relevant,
irrelevant and cross-lingual documents.

| check | result |
| --- | --- |
| token ids this service feeds, vs the reference's | identical, all twelve pairs |
| score, `sigmoid(logit)` vs official P(yes) | max abs difference **4.94e-4** |
| logit vs official `yes − no` | max abs difference 0.0132 |
| ranking per query, grouped and batched through the service | identical for all four queries |

The graph is fp16, which is where the 5e-4 comes from. The only other export found,
`n24q02m/Qwen3-Reranker-0.6B-ONNX` `model_yesno_quantized` (dynamic int8), was measured too
and rejected: its scores were up to **0.96** away from the official ones, with either output
order.

## Alternatives considered

**A second engine inside `inferences-embedding`.** Rejected by the order and by the
lifecycle: one resident model per worker, and Hindsight must be able to have both at once.

**Copying the embedding service's batching and TEI plumbing.** Rejected: two copies drift.
Both services now compose `packages/pylibs/textinfer`, extracted in this change with no
behaviour change to the embedding worker.

**The official safetensors under torch.** Rejected: torch. The exported graph plus
`tokenizers` reproduces the official scores to 5e-4.

**Longer inputs.** Measured at 4096 tokens: 84 s for one pair and a 4.7 GB peak. At 2048, a
5.1 GB peak and slower batches than at 1024. 1024 covers a memory and its query with room.

## Deployment and the cutover

`services/inferences-reranker/compose.yaml`: container `inferences-reranker` on the
external `proxy` network, no published ports, the `--probe ready` exec healthcheck,
`mem_limit: 5g`, four CPUs, weights in the bind-mounted `models/` directory (gitignored,
kept by `.gitkeep`), the DIP socket in `../../run`. `PRELOAD_MODEL` loads the model at start
until the orchestrator drives this worker.

The memory service's environment, with **embeddings and reranking on two different
stacks** — which is the deployment here — is in the service
[README](../../../services/inferences-reranker/README.md#the-memory-service). Two things in
it are not optional:

- `HINDSIGHT_API_RERANKER_TEI_BATCH_SIZE` at most **32**, this service's
  `max_client_batch_size`. Hindsight's default of 128 would be refused with 422.
- `HINDSIGHT_API_RERANKER_MAX_CANDIDATES` far below Hindsight's default of 300. At about
  0.8 s per candidate on this CPU, 300 would take four minutes per recall.

## Cost

Measured in the deployed container (4 CPUs, `RERANKER_THREADS=4`), each candidate a memory
of about 100 words:

| candidates per recall | latency |
| --- | --- |
| 4, one line each | 1.3 s |
| 8 | 6.7 s |
| 16 | 13.5 s |
| 32 | 25.4 s |
| 32, every pair cut to 1024 tokens | 202–252 s |

Load: 25–42 s (graph optimisation of the fp16 graph). Resident: about 2.2 GB.

**Memory.** Batches at the 1024-token limit peak at 3.5–3.8 GB. A reload after a large batch
was **OOM-killed at a 4 GB limit** that the first load fitted in: glibc kept the unloaded
model's 1.9 GB of freed heap, and the new session was built on top of it. The fix is in the
worker package: after an engine is released, the manager collects and calls
`malloc_trim(0)`, which returned 1.9 GB to 86 MB, measured. With it, the same sequence plus
two further unload/reload cycles ran at 4 GB without a kill, but with the cgroup at its
ceiling; hence 5g.

## Testing

```bash
make test        # 33 tests, offline, no weights
make coverage    # 97%, floor 93 (the sibling's)
```

Blackbox first: `LifecycleTest` drives load and unload over a real DIP socket and watches
`/rerank`, `/info` and `/health` answer 503, 200, 503 over real HTTP. `RerankEndpointTest`
sends Hindsight's exact request and every refusal against a one-slot admission cap, so a
failure path that leaks its slot turns the next row into a 429. Behind them is the real
`Reranker`, a real in-memory tokenizer, and a fake graph whose logit is a count over the ids
it was fed.

Tables for the pure parts: the ids of a pair, truncation in both directions, the limits,
TEI parsing, the ordering, sigmoid against raw scores.

**Mutation**: 23 deliberate breakages of the logic, all caught — among them dropping the
prefix or suffix, swapping query and text, truncating the query, ignoring the direction,
sorting worst first, applying or skipping the sigmoid wrongly, and ignoring `auto_truncate`.

Not covered by the suite: the ONNX Runtime constructor, which needs weights; ranking quality
beyond agreement with the official model.

## Open questions

- [ ] **Q:** At 0.8 s per candidate, reranking is the slowest step of a recall. Would a
  better-quantised export (validated as above, not assumed) or a GPU change the candidate
  budget enough to matter? — *owner:* Dhira
- [ ] **Q:** The instruction is fixed per deployment. Hindsight's memories might score better
  with a memory-specific instruction; measuring that needs a labelled recall set.
- [ ] **Q:** DIP `infer` cannot carry scores, as it cannot carry vectors. Shared with the
  embedding service's open question.
