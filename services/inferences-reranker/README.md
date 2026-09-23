# inferences-reranker

> One cross-encoder in memory. The orchestrator decides when it is resident, over DIP; the
> memory service asks it to rank, over TEI's `/rerank`.

**Status: v0, in review.** The sibling of
[`inferences-embedding`](../inferences-embedding/README.md), composed from
[`packages/pylibs/worker`](../../packages/pylibs/worker),
[`dip`](../../packages/pylibs/dip) and [`textinfer`](../../packages/pylibs/textinfer). The
design, the verified TEI schema and the measurements are in the
[technical requirement](../../docs/inferences/reranker/%5B1%5Dtechnical-requirement.md).

## TL;DR

```bash
make -C services/inferences-reranker up      # its own stack, on the `proxy` network
```

- **`Qwen/Qwen3-Reranker-0.6B`**, multilingual (Indonesian and Japanese included), CPU only
- **TEI's `/rerank`**: `{"query", "texts", "truncate", "truncation_direction", "raw_scores",
  "return_text"}` in, `[{"index", "score"}]` best first out, at `http://inferences-reranker:8080`
- **Matches the official model**: identical token ids, scores within 5e-4, identical rankings
- **The orchestrator owns lifetime**: nothing resident is a 503, never an implicit load
- **Over-long memories are cut, not refused** (`auto_truncate`); only the document is cut
- **Weights are never committed and never baked in**: fetched into `models/`, pinned by sha256

## The memory service

Hindsight, on the same `proxy` network. Embeddings and reranking are **two different stacks**
here, so they are two URLs. The full block for its `compose.yaml`:

```yaml
      # Embeddings: services/inferences-embedding
      HINDSIGHT_API_EMBEDDINGS_PROVIDER: tei
      HINDSIGHT_API_EMBEDDINGS_TEI_URL: http://inferences-embedding:8080
      HINDSIGHT_API_EMBEDDINGS_QUERY_PREFIX: "search_query: "
      HINDSIGHT_API_EMBEDDINGS_PASSAGE_PREFIX: "search_document: "
      HINDSIGHT_API_EMBEDDINGS_TEI_BATCH_SIZE: "8"
      HINDSIGHT_API_EMBEDDINGS_MAX_CONCURRENT_REQUESTS: "2"
      # Reranking: services/inferences-reranker
      HINDSIGHT_API_RERANKER_PROVIDER: tei
      HINDSIGHT_API_RERANKER_TEI_URL: http://inferences-reranker:8080
      HINDSIGHT_API_RERANKER_TEI_BATCH_SIZE: "32"
      HINDSIGHT_API_RERANKER_TEI_MAX_CONCURRENT: "1"
      HINDSIGHT_API_RERANKER_TEI_HTTP_TIMEOUT: "60"
      HINDSIGHT_API_RERANKER_MAX_CANDIDATES: "32"
```

Then switch Hindsight to its `-slim` image. What each reranker line is for:

- **`TEI_URL`** names this container. Were one stack ever to serve both, the two URLs would
  simply be the same; nothing else changes. They are not the same here: the embedding
  container has no `/rerank` and would answer 404.
- **`TEI_BATCH_SIZE: 32`** is this service's `max_client_batch_size`, which `/info` reports.
  Hindsight's default of 128 is refused with 422 `Validation`.
- **`MAX_CANDIDATES: 32`**, not Hindsight's default of 300. Measured in the deployed
  container with memories of about 100 words: 8 candidates take 6.7 s, 16 take 13.5 s, 32 take
  25.4 s. 300 would take about four minutes per recall. Lower it before raising anything else.
- **`TEI_MAX_CONCURRENT: 1`**: one lock serialises the work here, so parallel requests only
  queue, and past four in flight the answer is 429.
- **`TEI_HTTP_TIMEOUT: 60`**, because 32 candidates take 25 s; Hindsight's default is 30.
- The embedding lines are the sibling's; its README says why each one is what it is.

## How to use it

### Deployed

```bash
make up        # docker compose -f compose.yaml up -d --build
make down
```

`compose.yaml` runs `inferences-reranker` on the external `proxy` network with no published
ports, `mem_limit: 5g` (measured peak 3.5–3.8 GB), four CPUs, and
`PRELOAD_MODEL: qwen3-reranker-0.6b`, which stands in for the orchestrator until it drives
this worker. Weights land in `services/inferences-reranker/models/`, a bind mount whose
contents are gitignored; the DIP socket in `run/`.

### From the orchestrator

DIP at `/run/dita/inferences-reranker.sock`, exactly as for the sibling services:

```python
import dip

with dip.Requester.connect("run/inferences-reranker.sock", 300) as worker:
    worker.unload()
    worker.load("qwen3-reranker-0.6b")
```

`infer` over DIP is refused: its response shape cannot carry scores.

### From a TEI client

```bash
curl -s http://inferences-reranker:8080/rerank -H 'Content-Type: application/json' \
    -d '{"query": "Apa ibu kota Jepang?", "texts": ["東京は日本の首都です。", "Nasi goreng."]}'
```

With nothing resident the answer is 503 `Unhealthy`; with more than 32 texts, 422; with an
empty list, 400. `truncate: false` refuses a pair past 1024 tokens instead of cutting it.

### Locally

```bash
uv sync --package inferences-reranker
make -C services/inferences-reranker run    # TEI on 127.0.0.1:8081, socket in ../../run
```

Flags and env vars are the worker package's. This service adds `RERANKER_THREADS` (ONNX
Runtime intra-op threads; match it to the container's CPUs) and `RERANKER_LOG_LEVEL`.

## Contributions

```bash
make test          # 33 tests, offline, no weights
make coverage      # floor 93, the sibling's
make image
```

`tests/test_service.py` is blackbox: a real DIP socket and a real HTTP port, with a real
`Reranker` behind them on a fake graph. `tests/test_engine.py` holds the pair's exact ids to
the model card's prompt; `tests/test_tei.py` holds parsing and ordering to TEI's source. The
comparison with the official model uses the real weights and is recorded in the technical
requirement rather than run by the suite.
