# Spec — `services/inferences-ocr` (implement exactly this)

Companion to `.claude/tasks/todo.md` (the plan + verification list). Rationale/design rationale lives
outside this repo at `~/second-brain/wiki/homelab/research/[4]ocr-local-research-and-options.md`.

## Goal

The first Python **inference worker** of the dita monorepo: a long-lived process that loads **exactly one**
OCR model at a time and answers requests over a **unix socket**. The Go orchestrator owns the client-facing
HTTP, the queue, and the decision of *what* to load; this service only does tensors.

## Deliverables (exact paths)

1. **`services/inferences-ocr/models.yaml`** — the registry (committed). One entry per selectable model:
   `id`, `description`, `engine`, `langs`, `source` (huggingface repo + `revision`), `files[]` (with
   `sha256` + `bytes`), and any engine options. Seed three entries:
   - `rapidocr-ppocrv5` — **the default**, ONNXRuntime-only, JA + EN.
   - `tesseract` — the system binary (no download; `engine: tesseract`, note the required apt package).
   - `manga-ocr` — Japanese vertical text (JA specialist).
   Mark clearly which fields are verified vs placeholders (see Rules — never invent a sha256).

2. **`services/inferences-ocr/`** Python service (module layout is yours; keep names obvious):
   - **model manager** — `list()` / `load(id)` / `unload()` / `resident()`. **Invariant: at most ONE model
     resident.** `load()` unloads the previous one first. Guard with an exclusive lock; make `infer` fail
     with a clear error when nothing is loaded.
   - **fetcher** — download a model's files from the source into `MODELS_DIR`, verify `sha256`, skip when
     already present and valid. Must not silently accept a mismatch.
   - **unix-socket server** — `AF_UNIX` / `SOCK_SEQPACKET` at `$SOCKET_PATH`. Requests are a small JSON
     control block; `infer` carries raw image bytes. Ops: `handshake`/`version`, `list`, `load`, `unload`,
     `infer`. Return structured JSON. If `SOCK_SEQPACKET` interop proves awkward, fall back to a
     length-prefixed `SOCK_STREAM` and **document the change**.
   - **infer** — image bytes → `{text, lines: [{text, confidence, box}]}` using the resident model.
   - **dev HTTP mode** — off by default (`--http`), for manual testing only.
   - `requirements.txt` — **pinned with upper bounds** (`>=x,<next_major`). No torch.
   - `Dockerfile` — slim base, non-root user, no torch. Mount points: models dir + the socket dir.

3. **`deployment/docker-compose.yml`** — add the service: the shared socket dir, the models volume,
   resource limits (start at `mem_limit: 2g`, `cpus: 1.0`), `restart: unless-stopped`.

4. **`services/inferences-ocr/README.md`** — what it is, the registry + *how to add a model*, how to run,
   **the wire protocol** (ops + framing), and a short **"How Go integrates"** section: the orchestrator
   speaks the same socket, owns the queue/resource budget, and decides what to load.

5. **`README.md`** (repo root, currently empty) — a short index of services + the Go↔Python contract in brief.

## Rules (non-negotiable)

- **Never commit model weights.** `models/` is a mounted volume; gitignore it if needed.
- **Do not invent hashes or sizes.** If a sha256 isn't verified, leave it explicitly `null`/`TODO` and say so.
- **Do not touch** `services/dita-orchestrator/` or anything outside the deliverables above (plus
  `.gitignore` if required).
- **Simplicity first** — no plugin framework, no speculative abstraction beyond the registry.
- Git: work on branch **`feat/inferences-ocr`**; commit identity `Dhira Wigata <dwigata.putra@gmail.com>`
  (set it locally if unset). **Do not push, do not open a PR** — leave the branch local for review.
- This repo is **public**: no secrets, keys, or tokens anywhere.

## Verify + report honestly

Run what the environment allows and show **real output**: syntax/import checks, `docker build` if feasible,
and — if you can fetch one model and run a real inference on a generated test image — show the actual text
returned (EN **and** JA if the model supports it). Prove the one-model invariant (load A → load B → only B
resident). Anything you could not verify here, **say so explicitly** instead of claiming success.

Report back: files added · what you verified (with output) · what you could NOT verify · anything you'd flag.
