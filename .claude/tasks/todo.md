# Task: `services/inferences-embedding`

Order: `~/.hermes/tmp/dita-order-embedding.md`. Legend: `[ ]` todo · `[x]` done.

## Deviations from the order, decided up front
- **Branch and PR.** Work lives on `feat/inferences-embedding`, branched from `main`; the PR
  is opened from it and never merged here. `main` is not committed to.
- **Gemma weights come from `onnx-community/embeddinggemma-300m-ONNX`.** `google/embeddinggemma-300m`
  is gated (`gated: manual`) and the fetcher has no auth by design (no tokens in the repo).
- **The TEI surface lives on the worker's existing HTTP port** (the metrics server in
  `packages/pylibs/worker`), extended with service-supplied routes. No second server.
- **DIP `infer` refuses on this worker.** `InferResponse` is closed to `text`/`lines`; vectors
  do not fit without a spec change, which the order forbids. DIP carries the lifecycle.

## Steps
- [x] 1. `packages/pylibs/worker`: `ModelManager.run(work)` (engine under the lock, metrics
      recorded) and `Worker.routes` served on the HTTP port. Tests, floor held.
- [x] 2. `services/inferences-embedding`: pyproject, models.yaml (digests from real downloads),
      `onnx_embedder` engine (mean / last-token / graph pooling, Matryoshka, prefixes, token
      budgeted batches), tests.
- [x] 3. TEI routes: `POST /embed`, `GET /info`, `GET /health`, TEI error shapes and codes.
- [x] 4. Dockerfile, .dockerignore, Makefile, .coveragerc; image builds.
- [x] 5. `compose.yaml`: `proxy` network, no host ports, fixed name, healthcheck, mem cap,
      weights on a persisted volume.
- [x] 6. Docs: `docs/inferences/embedding/[1]technical-requirement.md`, README, the Hindsight env.

## Verification
- [x] Every file's sha256 in models.yaml verified by the real fetcher.
- [x] Each model's vectors checked against a reference implementation (sentence-transformers)
      where one can be run; the EmbeddingGemma card's published similarities where not.
- [x] Container up on `proxy`, `/embed` answered by name from another container.
- [x] Peak memory per model measured; cap chosen from it.
- [x] `make doctor`, `make test`, `make coverage`, `make py-verify`.
- [x] Mutation pass over the new tests.

## Review

- **Reference agreement** (max abs diff, fp32): nomic 1.7e-7 / 2.1e-7 / 2.7e-7 (query, document,
  256-d) against sentence-transformers 3.4.1; Qwen3 6.6e-7 / 4.8e-7 / 9.7e-7 against
  sentence-transformers 5; EmbeddingGemma 1.8e-7 against its card's published similarities.
- **Memory**, peak RSS at the shipped limits: nomic 2.2 GB, EmbeddingGemma 1.3 GB, Qwen3 5.2 GB
  (5197 MiB in the 6g container, no OOM). Qwen3 at 8000 tokens: 12.3 GB, host OOM kill.
- **Rejected on measurement**: Qwen3 int8 (cosine 0.48-0.87 to fp32); the ONNX Runtime arena
  (3.7 GB held after one long batch).
- **Mutation**: 20 on the service, 11 on the worker seam; all caught after two weak tests were
  fixed (truncation state, float spelling). One equivalent mutant discarded.
- **Found on the way**: adding a second workspace member broke OCR's image build (`uv export
  --locked` saw a lock with a member the deps stage could not). Fixed in its Dockerfile, ordered
  before the service commit. OCR's `services/inferences-ocr/.dockerignore` is inert (BuildKit
  reads `<Dockerfile>.dockerignore` or the context root's), proven with a two-file experiment;
  left alone as out of scope.
- **Not verified**: model quality beyond the reference agreement; Hindsight actually cut over
  (its compose was not touched); the orchestrator driving this worker (it has no client yet).

---

# Task: `services/inferences-reranker`

Order: `~/.hermes/tmp/dita-order-reranker.md`. Branch `feat/inferences-reranker`, stacked on
`feat/inferences-embedding` (PR #11): it needs the worker's route seam, not yet on `main`.

## Steps
- [x] 1. Verify TEI's `/rerank` schema in its source (commit `29ccc53`) and Hindsight's client.
- [x] 2. Find an ONNX export; prove it against the official model in fp32 before trusting it.
- [x] 3. Extract what two services share into `packages/pylibs/textinfer`; embedding composes it.
- [x] 4. `services/inferences-reranker`: models.yaml, `onnx_cross_encoder`, TEI routes, tests.
- [x] 5. Dockerfile and `compose.yaml` on `proxy`; mem cap from measurement.
- [x] 6. Docs under `docs/inferences/reranker/`, the two-stack Hindsight cutover.

## Review
- **Export**: `shawnw3i/…-seq-cls-ONNX` (fp16): ids identical to the reference, scores within
  4.94e-4, every ranking equal. `n24q02m/…-ONNX` int8: up to 0.96 off, rejected.
- **Found on the way**: a reload OOM-killed at 4 GB because glibc kept the unloaded model's
  1.9 GB of freed heap. Fixed in the worker package (`malloc_trim` after release; 1900 → 86 MB),
  which every worker gets. Cap set at 5g: at 4g the cgroup sat at its ceiling.
- **Mutation**: 23 on the service, 15 on `textinfer`, 4 on the trim; all caught after three
  weak tests were fixed (session options, refuse-not-wait, and one meaningless test deleted).
- **Not verified**: Hindsight actually cut over; the orchestrator driving it; ranking quality
  beyond agreement with the official model.
