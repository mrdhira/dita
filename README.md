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

- **Transport is a unix socket**, `AF_UNIX` / `SOCK_SEQPACKET`, on a docker volume shared
  between the orchestrator and the workers (`/run/dita/<service>.sock`). No HTTP between
  Go and Python. Each worker has a dev-only HTTP mode behind a flag, off by default, which
  is not part of the contract.
- **Framing** rides on SEQPACKET's message boundaries: a small prologue datagram giving
  the length of the JSON control block and of the binary payload, then both of those in
  chunks of at most 64 KiB. Both are chunked because a single AF_UNIX datagram cannot
  exceed `SO_SNDBUF` (212992 bytes by default) — and a control block listing a dense page's
  OCR lines reaches that ceiling as readily as an image does. From Go that is
  `net.Dial("unixpacket", ...)` with one `Write`/`Read` per datagram and no `bufio`.
- **Ops** are `handshake`/`version`, `list`, `load`, `unload`, and the worker's own
  inference op (`infer` for OCR). Every response carries `ok`; failures carry a stable
  `error.code` plus a human `error.message`. `handshake` advertises the chunk size, the
  size ceilings and the socket timeouts, so no client hard-codes them.
- **One model resident per worker.** Several models are *selectable*; never two loaded.
  `load` evicts whatever was there and reports what it evicted, so the orchestrator can
  budget memory as the largest single model rather than the sum. The download happens
  before the eviction, so a failed fetch leaves the working model serving.
- **The orchestrator owns the queue and the choice of model.** Workers hold an exclusive
  lock and serialise work behind it; they never shed load, and they never load a model
  implicitly. Timeouts, retries, rate limiting and backpressure are the Go side's job.
- **Weights are never committed.** Each worker ships a `models.yaml` pinning every file to
  an immutable upstream revision with a sha256; the worker fetches into a mounted volume
  and refuses anything whose digest does not match.

## Repo conventions

Working notes, plans and specs live under `.claude/tasks/`. Agent guidance is in
[`AGENTS.md`](AGENTS.md); the per-language rule files under `.claude/rules/` are still empty.
