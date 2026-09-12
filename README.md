# dita

A small self-hosted AI stack: one Go control plane in front of several Python inference
workers. The Go side owns everything client-facing — HTTP, queueing, resource budget, and
the decision about which model each worker should hold. The Python workers own tensors and
nothing else.

## Services

| path | language | what it is | status |
| --- | --- | --- | --- |
| [`services/dita-orchestrator`](services/dita-orchestrator) | Go | The control plane. Client-facing REST on `:2104`, chat handling, and the client for every inference worker. | running |
| [`services/inferences-ocr`](services/inferences-ocr/README.md) | Python | OCR worker. PP-OCRv5 (ja/en/zh) by default, plus tesseract and manga-ocr. ONNXRuntime only. | **this is the first worker** |
| `services/inferences-stt` | Python | Speech to text. | placeholder |
| `services/inferences-tts` | Python | Text to speech. | placeholder |

Shared Go packages live under `packages/`. Deployment lives in
[`deployment/docker-compose.yml`](deployment/docker-compose.yml).

```bash
docker compose -f deployment/docker-compose.yml up --build
```

## The Go ↔ Python contract, in brief

Every inference worker follows the same shape. `services/inferences-ocr` is the reference
implementation; its [README](services/inferences-ocr/README.md) has the full protocol.

- **Transport is a unix socket**, `AF_UNIX` / `SOCK_SEQPACKET`, in a directory shared
  between the orchestrator and the workers (`run/` in the repo, `/run/dita` in the
  containers). There is no HTTP between Go and Python at all, health probes included —
  the reasoning is in the OCR worker's technical requirement.
- **Framing** rides on SEQPACKET's message boundaries: a small prologue datagram giving
  the length of the JSON control block and of the binary payload, then both of those in
  chunks of at most 64 KiB. Both are chunked because a single AF_UNIX datagram cannot
  exceed `SO_SNDBUF` (212992 bytes by default) — and a control block listing a dense page's
  OCR lines reaches that ceiling as readily as an image does. From Go that is
  `net.Dial("unixpacket", ...)` with one `Write`/`Read` per datagram and no `bufio`.
- **Ops** are `handshake`/`version`, `list`, `load`, `unload`, the worker's own inference
  op (`infer` for OCR), and the three health probes `livez`, `readyz` and `startupz`.
  Every response carries `ok`; failures carry a stable `error.code` plus a human
  `error.message`. `handshake` advertises the chunk size, the size ceilings and the socket
  timeouts, so no client hard-codes them.
- **One model resident per worker.** Several models are *selectable*; never two loaded.
  `load` evicts whatever was there and reports what it evicted, so the orchestrator can
  budget memory as the largest single model rather than the sum. The download happens
  before the eviction, so a failed fetch leaves the working model serving.
- **The orchestrator owns the queue and the choice of model.** Workers hold an exclusive
  lock and serialise work behind it; they never shed load, and they never load a model
  implicitly. Timeouts, retries, rate limiting and backpressure are the Go side's job.
- **Weights are never committed.** Each worker ships a `models.yaml` pinning every file to
  an immutable upstream revision with a sha256; the worker fetches into a bind-mounted
  `models/` directory and refuses anything whose digest does not match. The directory is
  kept in git by a `.gitkeep`, its contents are gitignored, and `rm -rf` on it is a full
  reset.
- **Health is three protocol ops, not three URLs.** `livez` means restart me, `readyz`
  means stop routing to me, `startupz` means I am still booting. A container healthcheck
  is an exec probe: `python -m ocr_worker --probe ready`.

A stdlib-only Go reference client lives in
[`services/inferences-ocr/examples/go`](services/inferences-ocr/examples/go/) and is the
shape the orchestrator's client will take.

## Docs

Design documents live under `docs/`, one directory per service:

- [`docs/inferences/ocr/[1]technical-requirement.md`](docs/inferences/ocr/%5B1%5Dtechnical-requirement.md)
  — the OCR worker: context, the wire protocol, the per-engine pipeline boundaries, the
  alternatives that lost, and the rollout.

## Repo conventions

Working notes and plans live under `.claude/tasks/`; anything durable graduates to `docs/`.
Agent guidance is in [`AGENTS.md`](AGENTS.md); the per-language rule files under
`.claude/rules/` are still empty. Repo-level tasks are in the [`Makefile`](Makefile)
(`make build`, `make test`, `make deps-check`).
