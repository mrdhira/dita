# Task: first inference service — `services/inferences-ocr`

Plan for the OCR inference worker + the Go integration contract. Legend: `[ ]` todo · `[x]` done.

## Context
- Repo `mrdhira/dita` (public). Go control plane = `services/dita-orchestrator`; Python workers = `services/inferences-*`.
- Decisions + model research: `second-brain/wiki/homelab/research/[4]ocr-local-research-and-options.md`.
- Repo mandate (`AGENTS.md`): plan → **check in** → implement → verify → PR. `GO-CODE-GUIDELINES.md` and `PYTHON-CODE-GUIDELINES.md` are currently **empty** (no language rules yet).

## Decisions (settled)
- **Service: OCR** — the only missing capability; models are 10–20 MB; ONNXRuntime only (no torch).
- **Weights are NOT committed.** A `models.yaml` manifest in-repo pins each model (`id → source + revision + sha256 + bytes + engine + langs`); weights are fetched into a **mounted `models/` volume** and cached.
- **One model resident per service.** An exclusive `ModelManager`; several models are *selectable*, never co-resident.
- **Transport: `AF_UNIX` / `SOCK_SEQPACKET`** on a shared volume. HTTP exists only as an off-by-default dev flag.

## Steps
- [x] 1. Branch `feat/inferences-ocr` from `main`.
- [x] 2. `services/inferences-ocr/models.yaml` — seed registry: RapidOCR(PP-OCRv5) default, Tesseract, Manga-OCR (JA).
- [x] 3. `services/inferences-ocr/` — Python service:
  - [x] 3a. `ocr_worker/manager.py` — `list` / `load(id)` (unloads previous) / `unload` / resident state + lock
  - [x] 3b. `ocr_worker/fetcher.py` — download from HF → sha256 verify → cache under `MODELS_DIR`
  - [x] 3c. `ocr_worker/server.py` + `protocol.py` — UDS (`SOCK_SEQPACKET`): `handshake/version` · `list` · `load` · `infer` · `unload`
  - [x] 3d. `infer` path — image bytes → text (+ boxes/confidence) via RapidOCR (also Tesseract and manga-ocr)
  - [x] 3e. `requirements.txt` — pinned with upper bounds (rapidocr, onnxruntime, numpy, pillow, pyyaml, opencv)
  - [x] 3f. `Dockerfile` — `python:3.12-slim-bookworm`, non-root, no torch
- [x] 4. `deployment/docker-compose.yml` — add the service (shared socket volume, models volume, mem/cpu caps, restart policy).
- [x] 5. `services/inferences-ocr/README.md` — purpose, model registry, adding a model, running it, the wire protocol.
- [x] 6. Root `README.md` (was empty) — service index + the Go↔Python contract in brief.
- [x] 7. Go integration: **explained only** — see "How Go integrates" in the service README.
- [x] 8. Verify (see below), secret-scan. **Branch stays local**: the spec says do not push and do not open a PR.

## Verification — must show real output, not claims
- [x] `docker compose -f deployment/docker-compose.yml build` succeeds (both services).
- [x] `list` returns the registry; `load` fetches + verifies the sha256; a **second `load` unloads the first**.
- [x] OCR returns correct text for a known test image (EN **and** JA).
- [x] Resident-model invariant holds — never two loaded at once (unit test asserts it on the objects, including under 24 concurrent loads).
- [x] `git status` shows **no weights** staged (only code + manifest).
- [x] A corrupt cached file is never silently accepted (proved with an unreachable source).
- [x] Tesseract Japanese quality measured: English is clean, Japanese is not (see the review section). Documented rather than hidden.

## Out of scope (this PR)
- The Go orchestrator client (contract documented; wiring later).
- Shared-memory / fd-passing fast path (v2).
- The fine-tuning pipeline (later — but the storage fields for it are designed now).

## Review section

### What changed
| path | what |
| --- | --- |
| `services/inferences-ocr/models.yaml` | The registry. Three models, every digest real. |
| `services/inferences-ocr/ocr_worker/` | `registry.py`, `fetcher.py`, `manager.py`, `protocol.py`, `server.py`, `http_dev.py`, `__main__.py`, `engines/{base,rapidocr_engine,tesseract_engine,manga_ocr_engine}.py`. |
| `services/inferences-ocr/tests/test_worker.py` | 14 stdlib-only tests, no network, no weights. |
| `services/inferences-ocr/{requirements.txt,Dockerfile,.dockerignore,README.md}` | Pins, image, docs. |
| `deployment/docker-compose.yml` | The `inferences-ocr` service + the two volumes. |
| `README.md`, `.gitignore` | Repo index and the Go↔Python contract; ignore `models/` and Python artefacts. |

### Evidence
- **EN + JA on the default model** (generated 900x300 test image, PP-OCRv5):
  `"Hello dita OCR 2026\nThe quick brown fox\n日本語のテキスト認識"` at 0.968 / 0.991 / 0.9996.
  All three lines correct, boxes returned.
- **manga-ocr on vertical Japanese** (a 120x400 bubble crop): `今日はいい天気ですね`, confidence 0.99997.
- **Tesseract on the same EN+JA image**: English perfect, Japanese came back
  `AA 告 の テキ ス ト 認識` instead of `日本語のテキスト認識`. Kept as a fallback, not a
  recommendation — the README says so.
- **One-model invariant**: load A → load B returns `"unloaded": "rapidocr-ppocrv5"` and
  `resident` shows only B; reversed the same way. The unit test asserts it on the engine
  objects (the previous engine is `close()`d before the next is built) and holds under 24
  concurrent loads.
- **Cold fetch inside the container** against an empty models volume: 4 files downloaded,
  sha256 of both ONNX files matches `models.yaml` byte for byte.
- **Corrupt cache**: flipped 9 bytes in a cached `.onnx` and pointed `HF_ENDPOINT` at a dead
  port. `load` failed with `fetch_failed`, `infer` then failed with `no_model_loaded`. The
  bad bytes were never loaded.
- **Image**: 1.14 GB, non-root (`uid=10001(ocr)`), `pip list | grep -iE 'torch|paddle|cuda'`
  returns nothing.

### Review round 2 — fixes applied

An independent review found one real defect and a set of hardening items. What changed:

**HIGH — oversized responses died with EMSGSIZE.** The control block was sent as a single
datagram while only payloads were chunked, so an `infer` result past `SO_SNDBUF` (212992
bytes) failed to send, the connection dropped, and the orchestrator saw a bare EOF. The
framing is now a small prologue datagram (`protocol`, `control_len`, `payload_len`) followed
by **both** sections chunked at 64 KiB. Protocol bumped to **version 2**. `handshake` now
advertises a `limits` block (chunk size, control and payload ceilings, both timeouts)
instead of a lone `max_chunk`, and the README's claim that a 1 MiB receive buffer prevents
truncation is gone — the receive buffer is exactly one chunk and `MSG_TRUNC` is checked.

**MEDIUM.**
- Socket timeouts: 300s idle between messages, 30s for a half-sent one, 30s on send.
  A stalled peer gets a `timeout` error and the thread is released. Concurrent connections
  are capped at 16; number 17 gets `busy` and a drain window so it can actually read it.
- `load` now fetches **outside** the exclusive lock and **before** releasing anything.
  Inference keeps running during a download, and a failed fetch leaves the working model
  resident. `list`/`handshake` gained a `loading` field.
- Compose orders `inferences-ocr` first via `depends_on`, so the shared socket volume takes
  its ownership from the worker's image rather than from whichever container won the race.
  The worker also checks the directory at startup and explains what to change if it cannot
  bind, instead of crash-looping on a bare EACCES.

**LOW (the ones asked for).** `resident` stays accurate during a load; `unloaded` is present
on the `already_resident` path; model ids are validated as safe directory names; downloads
are cut off at the pinned size while streaming; `HF_ENDPOINT` must be http or https; a file
verified once this run is not re-hashed on the next load; `tesseract --list-langs` exit
status is checked.

**Not fixed, by instruction** (the review's remaining low items, left for a follow-up): the
bind-then-chmod window, manga-ocr's missing decoder KV cache, the dev HTTP mode discarding
query strings, and the `VOLUME` directive in the Dockerfile.

**Tests** went from 14 to 36. New: the oversized-response path as a unit test and again
end-to-end through the real `SocketServer`; a peer that announces `payload_len` and sends
fewer datagrams; a peer that closes mid-message; an oversized datagram; announced lengths
past the ceilings; the connection cap; registry id traversal; download size cap; endpoint
scheme; the re-hash memo; the full `dispatch` surface; a failed fetch not evicting; and
inference running during a download. The reviewer's two test criticisms are fixed:
`FakeEngine` is keyed by instance rather than engine name, and the concurrency test samples
live engines from *inside* the critical section.

### Round 2 evidence

- **Before/after on the same image.** A dense 3600x4400 page yields 900 lines and a 221317
  byte response, against an `SO_SNDBUF` of 212992. The pre-fix worker, run from the previous
  commit against the same models: `could not answer peer: [Errno 90] Message too long`, and
  the client saw "server closed the connection without answering". The fixed worker returns
  all 900 lines, in the container too.
- **Stalled peer**: announced four chunks, sent one, got
  `{"code": "timeout", "message": "peer sent nothing for 30.0s"}` and the worker kept serving.
- **Connection cap**: 16 accepted, the 17th answered `busy`, and a new connection succeeded
  after one was closed.
- **Integrity paths in the container**: corrupt cache with an unreachable source gives
  `fetch_failed`; `HF_ENDPOINT=file:///etc` is refused before any request.
- **36 tests pass** on the host and inside the image.
- EN+JA, vertical JA and tesseract outputs are unchanged from round 1.

### Flags for the reviewer
1. **`SOCK_SEQPACKET` was kept**, but nothing large fits in one AF_UNIX datagram
   (`SO_SNDBUF` caps them at 212992 bytes). Rather than falling back to `SOCK_STREAM`, a
   message is a small prologue datagram followed by the control block and the payload, each
   chunked at 64 KiB. SEQPACKET's boundaries are what make that framing need no per-chunk
   header. Go's `net.Dial("unixpacket", ...)` maps onto it directly.
2. **`models.yaml` allows a per-file `repo`/`revision` override.** PP-OCRv5 ships detection
   and recognition as two separate PaddlePaddle repos, so one model is assembled from two.
3. **The compose change touches the orchestrator service block** — one added volume mount for
   the shared socket directory. Without it "shared socket dir" is not true of anything.
   Nothing under `services/dita-orchestrator/` was modified.
4. **`tests/` was added** even though the spec did not list it. Small, stdlib-only, and it is
   what makes the one-model invariant checkable rather than merely asserted.
   36 tests after the review round.
5. **manga-ocr is ~460 MB** and peaks around 800 MB resident, against a `mem_limit: 2g`. It
   fits, but it is the reason to leave the limit where it is rather than trim it.
6. **`rapidocr` pulls `opencv_python`**, which wants `libgl1`/`libglib2.0-0` in the image.
   That, plus the tesseract language data, is most of the 1.14 GB.
7. **A failed engine *build* still leaves nothing resident**, deliberately. The fetch now
   happens first, so a network failure is harmless; but once the swap starts, the old engine
   is released before the new one is constructed. Keeping both alive to allow a rollback
   would break the one-model invariant, which the spec calls non-negotiable.
8. **The download lock is separate from the exclusive lock.** Two callers cannot fetch at
   once, but a fetch never blocks inference. There is still no cancel op for a load in
   flight; a Go-side timeout abandons the response, not the download.
