# inferences-embedding

> One text embedding model in memory. The orchestrator decides which, over DIP; the memory
> service asks it for vectors, over TEI's `/embed`.

**Status: v0, in review.** Second inference worker in the dita monorepo, composed from
[`packages/pylibs/worker`](../../packages/pylibs/worker) and
[`packages/pylibs/dip`](../../packages/pylibs/dip) the way
[`inferences-ocr`](../inferences-ocr/README.md) is. The design, the measurements and the
reasoning are in the
[technical requirement](../../docs/inferences/embedding/%5B1%5Dtechnical-requirement.md).

## TL;DR

```bash
make -C services/inferences-embedding up      # its own stack, on the `proxy` network
```

- **Three models, one resident:** `nomic-embed-text-v1.5` (default, **English only**),
  `embeddinggemma-300m` and `qwen3-embedding-0.6b` (multilingual, 100+ languages)
- **TEI-compatible**: `POST /embed` with `{"inputs": [...]}`, `GET /info`, `GET /health`,
  TEI's error bodies and status codes, on `http://inferences-embedding:8080`
- **The orchestrator owns lifetime.** `load` / `unload` over DIP; `/embed` with nothing
  resident is a 503, never an implicit load
- **ONNX Runtime and `tokenizers` only.** No torch. Vectors match sentence-transformers to 1e-6
- **Weights are never committed and never baked in**: fetched into `models/`, pinned by sha256

## The models

| id | languages | dims | served tokens | query / document prompts |
| --- | --- | --- | --- | --- |
| `nomic-embed-text-v1.5` | **en only** | 768 (512, 256, 128, 64) | 2048 | `search_query: ` / `search_document: ` |
| `embeddinggemma-300m` | 100+ | 768 (512, 256, 128) | 2048 | `task: search result \| query: ` / `title: none \| text: ` |
| `qwen3-embedding-0.6b` | 100+, best on id and ja | 1024 (32–1024) | 2048 | `Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery:` / none |

**Do not select `nomic-embed-text-v1.5` for Indonesian or Japanese text.** It is English-only.
Load `qwen3-embedding-0.6b` for those; `embeddinggemma-300m` is the lighter multilingual
choice. A reranker is not served here yet; the planned one is `Qwen/Qwen3-Reranker-0.6B`.

Pooling, per model card: nomic is mean pooling (and layer-norm before a Matryoshka cut);
EmbeddingGemma pools, projects and normalises inside its graph; Qwen3 takes the last token.
Everything is L2-normalised unless the request says `"normalize": false`.

## The memory service

Hindsight, on the same `proxy` network. The exact block for its `compose.yaml`, with the
default model resident:

```yaml
      HINDSIGHT_API_EMBEDDINGS_PROVIDER: tei
      HINDSIGHT_API_EMBEDDINGS_TEI_URL: http://inferences-embedding:8080
      HINDSIGHT_API_EMBEDDINGS_QUERY_PREFIX: "search_query: "
      HINDSIGHT_API_EMBEDDINGS_PASSAGE_PREFIX: "search_document: "
      HINDSIGHT_API_EMBEDDINGS_TEI_BATCH_SIZE: "8"
      HINDSIGHT_API_EMBEDDINGS_MAX_CONCURRENT_REQUESTS: "2"
```

- **The prefixes are not optional for nomic**, and they belong on the client: Hindsight
  prepends them itself, and this service adds nothing when no `prompt_name` is sent, so
  they are never doubled. They must match the resident model; for the others use the
  prompts in the table above (for Qwen3 the passage prefix is empty).
- **Batch 8, two in flight**, not Hindsight's defaults of 32 and 8: its client reads with a
  30-second timeout, one lock serialises the work here, and past four in flight the answer is
  429. Measured in the deployed container, 8 passages of 400 tokens take 3.6 s on nomic and
  15.4 s on Qwen3, so with Qwen3 use `MAX_CONCURRENT_REQUESTS: "1"`.
- **`HINDSIGHT_API_RERANKER_*` does not point here.** There is no `/rerank` on this
  container; reranking is [`inferences-reranker`](../inferences-reranker/README.md#the-memory-service),
  whose README has the block for both.
- **The model is fixed for the life of the store.** Hindsight detects the dimension from
  its first `/embed` and indexes for it. Loading a different model means re-embedding
  everything; `x-model-id` on every response says which model answered.

Then switch Hindsight back to its `-slim` image.

## How to use it

### Deployed

```bash
make up        # docker compose -f compose.yaml up -d --build
make down
```

`compose.yaml` runs `inferences-embedding` on the external `proxy` network with no published
ports, `mem_limit: 6g` (Qwen3 peaks at 5.2 GB; nomic at 2.2 GB), four CPUs, and
`PRELOAD_MODEL: nomic-embed-text-v1.5`, which stands in for the orchestrator until it drives
this worker. Weights land in `services/inferences-embedding/models/`, a bind mount whose
contents are gitignored; the DIP socket in `run/`, where the orchestrator dials it.

### From the orchestrator

DIP, exactly as for OCR, at `/run/dita/inferences-embedding.sock`:

```python
import dip

with dip.Requester.connect("run/inferences-embedding.sock", 300) as worker:
    worker.load("qwen3-embedding-0.6b")     # evicts what was there, reports it
    worker.unload()
```

`infer` over DIP is refused: its response shape cannot carry vectors.

### From a TEI client

```bash
curl -s http://inferences-embedding:8080/embed \
    -H 'Content-Type: application/json' \
    -d '{"inputs": ["search_query: who made TSNE?"], "dimensions": 256}'
```

`prompt_name` (`"query"`, `"document"`, …) prepends the resident model's prompt server-side
instead. `truncate: true` cuts an input past the limit rather than refusing it. At most 32
inputs per request.

### Locally

```bash
uv sync --package inferences-embedding
make -C services/inferences-embedding run    # TEI on 127.0.0.1:8080, socket in ../../run
```

Flags and env vars are the worker package's: `--preload`, `--probe live|ready|startup`,
`MODELS_DIR`, `SOCKET_PATH`, `METRICS_ADDR`. This service adds `EMBEDDING_THREADS` (ONNX
Runtime intra-op threads; match it to the container's CPUs) and `EMBEDDING_LOG_LEVEL`.

## Contributions

```bash
make test          # 43 tests, offline, no weights
make coverage      # floor 93
make image
```

`tests/test_service.py` is blackbox: a real DIP socket and a real HTTP port, with a real
`Embedder` behind them on a fake ONNX session. `tests/test_engine.py` and `tests/test_tei.py`
are tables over the pure parts. The reference comparisons against sentence-transformers use
the real weights and are recorded in the technical requirement rather than run by the suite.

### Adding a model

1. Pin an immutable revision and a sha256 per file, as for OCR (`curl
   https://huggingface.co/api/models/<repo>` for the sha; LFS digests from the tree API; hash
   the small files yourself).
2. Read the model card for its pooling, prompts, dimensions and context, and check the ONNX
   graph's inputs and outputs. `onnx_embedder` feeds `input_ids`, `attention_mask`,
   `token_type_ids`, `position_ids` and empty `past_key_values.*`; anything else is refused
   at load.
3. Measure its peak memory at the `max_input_tokens` and `max_batch_tokens` you choose, and
   raise `mem_limit` if it is the new largest.
4. Compare its vectors with the reference implementation before calling it done.
