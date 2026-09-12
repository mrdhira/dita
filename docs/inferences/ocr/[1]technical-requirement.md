---
type: technical-design
status: in-review
owner: Dhira Wigata
product: dita
prd: n/a — internal infrastructure
date: 2026-09-12
tags:
  - dita
  - inferences
  - ocr
  - technical-design
---

# Technical Design — services/inferences-ocr

> House style that applies here: **stdlib-first, minimal dependencies.** The templ + HTMX,
> frozen-DB and strangler-fig rules are web-app conventions and do not apply — this is a
> single-purpose worker process with no database and no user-facing surface. One departure
> to justify: the worker is Python, not Go, because the ONNX and OCR ecosystem is. See
> Alternatives.

## Context

dita is a Go control plane (`services/dita-orchestrator`) in front of inference workers that
do not exist yet. `services/inferences-ocr`, `-stt` and `-tts` are empty directories. OCR is
the first one built, and it therefore sets the pattern the other two will copy.

What forced the design:

- **The models are small and CPU-only.** PP-OCRv5 mobile is 21 MB of ONNX. There is no GPU in
  this homelab and no reason to pull torch or Paddle in to run a 21 MB graph. ONNXRuntime is
  the whole runtime.
- **Several models are useful, one at a time.** Japanese horizontal text, Japanese vertical
  and manga text, and clean Latin text are three different jobs with three different best
  answers. But holding three models resident on a 2 GB budget is not possible, and is not
  necessary — a request names a job, and the job picks a model.
- **The orchestrator already owns the hard parts.** It has the HTTP surface, the request
  queue, and the resource budget. Duplicating any of that inside the worker would mean two
  places to reason about backpressure.
- **The repository is public.** Model weights cannot be committed, and nothing in the repo
  may carry a secret.

The decision record for the model choices themselves lives outside this repo at
`~/second-brain/wiki/homelab/research/[4]ocr-local-research-and-options.md`.

## Goals and non-goals

**Goals**

- A long-lived process that holds **at most one** OCR model and answers `infer` over a unix
  socket. The invariant is structural, not best-effort: two models must never be in memory.
- Model files fetched at runtime into a mounted directory, **pinned by sha256**, with an
  unverifiable file being unloadable rather than quietly trusted.
- A wire contract a Go client can implement in the standard library, in about forty lines.
- Steady-state `infer` on a full page in roughly one second on one CPU core.
- Failure that is legible: every error carries a stable code, and a peer never has to infer
  meaning from a dropped connection.
- Health semantics an orchestrator or a scheduler already understands.

**Non-goals**

- **Not a queue.** The worker serialises behind one lock and never sheds load (except past a
  connection cap). Rate limiting, retries, timeouts and backpressure stay in Go.
- **Not a model chooser.** It will not load a default implicitly on `infer`. A load costs
  hundreds of megabytes of download; that is a decision, not a side effect.
- **No HTTP surface**, not even for health. See Alternatives.
- **No GPU, no torch, no Paddle.** If a model needs them it does not belong in this service.
- **No fine-tuning pipeline** yet, though the registry's `source`/`revision`/`files` shape is
  designed so a locally trained model is just another entry.
- **No shared memory or fd passing.** A copy through the socket is affordable at these sizes.

## Design

```
  dita-orchestrator (Go)                 inferences-ocr (Python)
  ┌────────────────────┐                 ┌──────────────────────────────────┐
  │ HTTP :2104         │                 │ SocketServer  ── accept, ≤16 conns│
  │ queue, budget,     │  AF_UNIX        │      │                            │
  │ model policy       │─ SOCK_SEQPACKET─┼──► dispatch ──► ModelManager      │
  │                    │  /run/dita/...  │                   │  one lock      │
  └────────────────────┘                 │                   ▼                │
                                         │            Engine (exactly one)    │
                                         │     rapidocr │ tesseract │ manga   │
                                         │                   │                │
                                         │              fetcher ──► $MODELS_DIR│
                                         └──────────────────────────────────┘
```

The request path: a datagram arrives, `dispatch` turns one control block into one response,
and anything that touches the model goes through `ModelManager`'s exclusive lock. Nothing
else in the process holds state.

### Data model

No database. Two pieces of durable state, both on disk:

**`models.yaml`** — the committed registry, the only source of truth for what is selectable.

```yaml
version: 1
default_model: rapidocr-ppocrv5
models:
  - id: rapidocr-ppocrv5          # also the directory name under $MODELS_DIR
    description: ...
    engine: rapidocr              # rapidocr | tesseract | manga_ocr
    langs: [ja, en, zh]
    source:
      type: huggingface           # huggingface | system
      repo: PaddlePaddle/PP-OCRv5_mobile_det_onnx
      revision: e6f4fa85...       # an immutable commit sha, never a branch
    files:
      - path: inference.onnx      # path in the source repo
        dest: det/inference.onnx  # path under $MODELS_DIR/<id>/
        sha256: a431985659...     # null ⇒ the model is unloadable
        bytes: 4826518
      - repo: PaddlePaddle/PP-OCRv5_mobile_rec_onnx   # per-file override
        revision: ed152b8b...
        path: inference.onnx
        dest: rec/inference.onnx
        sha256: da72dc72...
        bytes: 16534782
    options: { ... }              # engine-specific, passed through untouched
```

The per-file `repo`/`revision` override exists because PP-OCRv5 publishes detection and
recognition as two separate upstream repositories; one logical model is assembled from two.

`id` is validated as a single safe path segment, because it becomes a directory name.
`dest` is validated against traversal for the same reason.

**`$MODELS_DIR/<id>/…`** — the fetched files, a bind-mounted repo directory
(`services/inferences-ocr/models/`). Gitignored contents, `.gitkeep` for the directory. It is
a cache, never a source of truth: deleting it costs a download and nothing else.

In-memory state is one engine, one `(spec, loaded_at)` tuple assigned as a unit so a lock-free
reader cannot see a half-updated pair, a `loading` id, and a sticky `last_error` from the most
recent failed load.

**The dependency model** is the third piece of committed state, and it is repo-wide rather
than service-local. The root `pyproject.toml` is a **virtual uv workspace root** — no
`[project]` table, only `[tool.uv.workspace]` with `members = ["services/inferences-*"]` — so
every Python service resolves together into a single `uv.lock` at the repo root. Bounds are
declared per service (`services/inferences-ocr/pyproject.toml`); exact versions and hashes
live only in the lock. There is no `requirements.txt`.

Three consequences, all deliberate:

- **The Docker build context is the repo root.** The lock lives at the top and a build
  context cannot reach outside itself, so `deployment/docker-compose.yml` sets
  `context: ..` with `dockerfile: services/inferences-ocr/Dockerfile`. The image still
  copies only this service's manifest, registry and source.
- **The image installs from the lock, not from a loose file.** A first build stage runs
  `uv export --package inferences-ocr --locked --no-emit-project`, producing a pinned
  requirements file where every entry carries a sha256; the runtime stage installs it with
  `pip --require-hashes`. uv itself never reaches the runtime image.
- **`--locked` is the staleness guard**, and it is what allows dependency updates to be a
  single tool. It verifies the lock against the manifests, so editing a bound without
  re-running `uv lock` fails the build. `--frozen` is *not* equivalent: it declines to update
  the lock but performs no check, exporting a stale lock and exiting 0. This was measured,
  not assumed.

### Interfaces

**Transport.** `AF_UNIX` / `SOCK_SEQPACKET` at `$SOCKET_PATH`, mode `0660`, on a directory
shared with the orchestrator. Protocol version 2.

**Framing.** A message is a prologue datagram followed by two chunked sections:

```
datagram 0     : {"protocol": 2, "control_len": N, "payload_len": M}
next datagrams : the control block, chunks of at most 65536 bytes
next datagrams : the payload, chunks of at most 65536 bytes
```

Both sections are chunked because an AF_UNIX datagram cannot exceed `SO_SNDBUF` — 212992
bytes by default — above which `send` fails with `EMSGSIZE` rather than fragmenting. This is
not hypothetical for the control block: a dense page yields 900 detected lines and a 221 KB
response. SEQPACKET's preserved boundaries are what let the chunks need no per-chunk header;
a length of zero means that section sends no datagrams at all, and since no conforming
datagram is empty, an empty read means the peer closed.

**Ops.**

| op | request | response |
| --- | --- | --- |
| `handshake` / `version` | — | `service`, `version`, `protocol`, `limits`, `engines[]`, `ops[]`, `default_model`, `resident`, `loading` |
| `list` | — | `models[]`, `default_model`, `resident`, `loading` |
| `load` | `id` | `id`, `engine`, `already_resident`, `load_ms`, `unloaded` |
| `unload` | — | `unloaded` |
| `infer` | payload = encoded image bytes, no fields | `text`, `lines[]`, `model`, `infer_ms` |
| `livez` / `readyz` / `startupz` | — | `probe`, `status`, `uptime_s`, `reasons[]` (+ `resident`, `loading` on `readyz`) |

Every response carries `ok`. A failure is `{"ok": false, "error": {"code", "message"}}`;
`code` is the stable contract and `message` is for humans. Codes: `bad_request`,
`unknown_model`, `unsupported_engine`, `checksum_mismatch`, `fetch_failed`,
`no_model_loaded`, `timeout`, `busy`, `response_too_large`, `internal`.

**Limits**, advertised in the handshake so nothing is hard-coded: `max_chunk` 65536,
`max_control` 8 MiB, `max_payload` 64 MiB, `idle_timeout_s` 300, `message_timeout_s` 30, and
a cap of 16 concurrent connections.

**Unknown fields are refused, not ignored.** Each op declares the fields it accepts
(`load` takes `id`, with `model` as an alias; everything else takes none) and anything else
is a `bad_request`. `infer` with a `model` field gets a message pointing at `load`.

**Idempotency and retries.** `load` is idempotent — loading what is already resident returns
`already_resident: true` and builds nothing. `unload` on an empty worker returns
`unloaded: null`. `infer` has no side effects and is safe to retry, but `checksum_mismatch`
and `fetch_failed` mean the model is unusable and only retrying the `load` can help.

**The engine boundary.** Each adapter implements `infer(bytes) -> Result` and `close()`.
Where the boundary falls differs per engine, and that asymmetry is deliberate — it reflects
how much of the pipeline each upstream already owns, not an inconsistency in our code.

| Engine | Upstream owns | We own |
| --- | --- | --- |
| **RapidOCR (PP-OCRv5)** | Resize and normalise, DBNet detection, box extraction and unclipping, optional angle classification, CRNN recognition, and the CTC decode: argmax over the per-timestep softmax, collapse repeats, drop the blank class, average the kept probabilities into a line confidence. | Choosing the pinned ONNX files instead of letting it download its own; supplying the label set; `use_cls: false`; `text_score: 0.5`; BGR channel order in; reshaping parallel tuples into `Result` out. Nothing else — reimplementing any of the above would only introduce bugs. |
| **Tesseract** | Everything. Binarisation, layout analysis, line finding, recognition, and its own confidence scores. | Shelling out and parsing TSV. We fold its one-row-per-word output back into lines, joining Japanese runs without spaces because spacing them corrupts the text. We normalise nothing. |
| **manga-ocr** | Nothing beyond two exported ONNX graphs and a vocabulary file. Upstream's pipeline lives in a torch `VisionEncoderDecoder`, which this service will not depend on. | **The whole pipeline.** Greyscale then three identical channels (what it was trained on), bilinear resize to 224×224, scale by 1/255, normalise with mean and std 0.5 — all read from the pinned `preprocessor_config.json`. One encoder pass. Then a greedy decode loop: the exported decoder has no key/value cache, so each step re-runs it over the whole prefix, takes the last timestep's logits, `argmax`es the next token, and stops at `eos_token_id` or `max_tokens`. Then id-to-vocabulary lookup (character-level tokenizer, so no mecab and no tokenizer library), whitespace and ellipsis normalisation. |

**On the softmax**, since it appears in the manga-ocr loop and nowhere else in our code:
`argmax` selects the next token straight from the logits and needs no normalisation. The
softmax exists **only** to convert that winning logit into a probability, so per-token values
are comparable and their mean is a meaningful line confidence. Without it the field would
carry a raw logit, which is on no scale a caller can reason about. RapidOCR performs the
equivalent step inside its own CTC decoder; we simply never see it. That is the whole of the
pre/post asymmetry: same operation, different side of the library boundary.

### Control flow

**Happy path, `load`.**

1. Look the id up in the registry. Unknown ⇒ `unknown_model`, nothing changes.
2. Already resident ⇒ return immediately, build nothing.
3. **Fetch, outside the exclusive lock and before releasing anything.** Under a separate
   fetch lock, each file is checked for size, then digest, then downloaded if needed and
   verified before being moved into place. Inference keeps serving from the current model
   throughout, and `loading` names the incoming one.
4. **Swap, under the exclusive lock.** Release the old engine, then construct the new one.
   Release-then-build is the invariant: keeping both alive to allow a rollback would mean two
   models in memory.
5. Clear `last_error`, record `(spec, now)`, return `unloaded` naming what was evicted.

**Failure modes.**

| What fails | What the caller sees | State afterwards |
| --- | --- | --- |
| Unknown model id | `unknown_model` | Unchanged; previous model still resident. |
| Download fails or times out | `fetch_failed` | **Unchanged.** The fetch precedes the release, so a transient network failure never evicts a working model. `last_error` is set. |
| Digest or size mismatch | `checksum_mismatch` | Unchanged; the bad file is deleted, never loaded. |
| Engine construction fails after a good fetch | the underlying error as `internal` | **Nothing resident.** Deliberate: the old engine was already released, and keeping it alive would break the invariant. `readyz` goes false. |
| `infer` with nothing resident | `no_model_loaded` | Unchanged. |
| Response over `max_control` | `response_too_large` | Connection stays open; the peer gets a code, not an EOF. |
| Peer stalls mid-message | `timeout`, then the connection closes | The thread and its buffers are released. |
| 17th concurrent connection | `busy`, then close after a drain window | Unchanged. |
| Socket directory unwritable at boot | process exits 3 with a remediation message | Not serving. `startupz` would be false. |

**Health, and what each verdict maps to.** The names follow the Kubernetes convention;
`/healthz` has been deprecated since v1.16 and is not offered.

- `livez` — process and accept loop are up. No dependency checks, nothing that can fail
  slowly. False ⇒ **restart me**.
- `readyz` — socket bound, registry parsed, models directory writable, and either something
  is resident or a load could still proceed. False ⇒ **stop routing to me**. A load in flight
  is progress, not a wedge, so a cold load does not make it false; `resident` in the response
  is what says whether an `infer` would succeed this instant. It goes false when the last
  load failed and nothing is resident, and recovers on the next successful load or unload.
- `startupz` — one-time boot work finished: socket bound, registry parsed. False ⇒ **do not
  kill me, I am still booting**.

Because there is no HTTP surface, these are protocol ops, and a container healthcheck is an
exec probe: `python -m ocr_worker --probe ready`, exiting 0 or 1.

## Alternatives considered

**HTTP, with FastAPI or similar.** Rejected. The orchestrator and the worker share a host, a
pod and a kernel. HTTP would add a TCP listener, an HTTP parser, a server dependency, and a
second transport to secure and keep in sync — to move bytes between two processes that can
pass them through a socket the kernel already owns. SEQPACKET gives message boundaries for
free, which is most of what a framing layer does, and filesystem permissions give access
control without a token scheme. The cost is that a client implements roughly forty lines of
framing; `examples/go/` is that, in the standard library, once. This is also why health is
three protocol ops rather than three URL paths: adding an HTTP listener *purely for probes*
would be the same trade at a worse ratio.

The measured numbers put the trade in proportion. A cold `load` is 6.1 s, almost all of it
download and digest verification; a full-page `infer` is 1.0–1.6 s on an uncapped host and
7.6 s under `cpus: 1.0`. Against a one-second unit of work, HTTP framing overhead is noise —
the objection is not latency, it is the dependency, the second transport, and the extra
surface to secure. The full cost table is in the service README.

**`SOCK_STREAM` with length prefixes.** Rejected, but narrowly. It is the obvious fallback
once you discover that a datagram cannot carry a whole image, and the original spec offered
it as an escape hatch. Keeping SEQPACKET means the chunks need no per-chunk header and a
short read is a protocol error rather than a silent partial parse. The prologue datagram
recovers the one thing SEQPACKET does not give: knowing how many datagrams a message spans.

**One process per model, or several models resident.** Rejected. Three resident models do not
fit a 2 GB budget, and three processes multiply the orchestrator's job by three while making
the memory budget a sum instead of a maximum. One resident model makes `mem_limit: 2g` a
promise rather than a hope.

**Committing the weights, or baking them into the image.** Rejected. The repository is
public, the manga-ocr weights alone are 460 MB, and a baked image cannot pick up a model that
did not exist at build time. A pinned manifest plus a runtime fetch gives reproducibility
without the bytes.

**A Go worker using an ONNX binding.** Rejected. The ONNX bindings for Go are CGO wrappers
around the same C++ runtime, and the OCR pre/post-processing ecosystem — RapidOCR, the
PP-OCR label handling, the manga-ocr export — is Python. Rewriting that in Go would be a
large amount of numerical code to get subtly wrong, against a service boundary that is one
socket wide. This is the one house-style departure, and the socket is what contains it.

**`pytesseract`.** Rejected. Tesseract is a system C++ binary; `pytesseract` shells out to
the same binary and re-parses the same TSV we already need for boxes. A dependency for
nothing.

**A flat `requirements.txt` plus a weekly "what is outdated" workflow.** This is what shipped
first, and it was two tools because one of them was half-blind: Dependabot could only move
what the file declared, so a transitive package — `antlr4-python3-runtime`, pinned exactly by
omegaconf, which rapidocr pulls in — was invisible to it and needed a workflow to report.
Replaced by the workspace lock, which puts every transitive package in a file Dependabot's
`uv` ecosystem reads and can update. The workflow is deleted; two mechanisms reporting on the
same dependencies is a way to have neither owned.

**A lockfile per service.** Rejected. Members of one monorepo that share a Python floor and
will share packages should resolve together, or two services can disagree about the version
of a shared transitive and nothing notices until both are in the same image. One lock also
gives Dependabot one place to look and produces one grouped PR instead of one per service.

## Migration and rollout

There is nothing to migrate. No existing OCR path, no data, no traffic to split.

Order of operations:

1. `docker compose up --build inferences-ocr`. The worker starts with **no model loaded** and
   an empty models directory, so the first start is fast and does no network I/O.
2. The orchestrator's `depends_on` waits for `condition: service_healthy`, which is the
   `readyz` exec probe. `start_period` is 30 s and only has to cover process start and the
   socket bind, because a cold load does not trip readiness.
3. The first `load` downloads and verifies (about 6 s for PP-OCRv5). Nothing is serving OCR
   before that point, and nothing depends on OCR yet.
4. The orchestrator client is a **follow-up change**. This design ships the worker and the
   contract; `examples/go/` is the reference the wiring will follow.

Point of no return: none. Until the orchestrator has a client, this service has no callers.

## Rollback

Cheap and total, at every stage.

- **Undo the service:** `docker compose stop inferences-ocr`. Nothing else depends on it
  until the orchestrator client lands, so nothing breaks.
- **Undo a bad model:** `unload`, edit `models.yaml`, `load` a different id. The registry is
  the only thing that decides what is selectable.
- **Undo a corrupt cache:** `rm -rf services/inferences-ocr/models/*`. It is a cache; the
  next load re-downloads and re-verifies. This is the whole reason it is a bind-mounted repo
  directory rather than a named volume.
- **Undo the branch:** the service is self-contained under `services/inferences-ocr/` plus a
  compose block. Nothing under `services/dita-orchestrator/` was modified.

Once the orchestrator routes OCR traffic here, rollback becomes "stop routing", which is what
`readyz` already expresses.

## Observability

The worker logs to stdout, which is where compose and any scheduler collect it.

- **Boot:** the socket path and protocol version at INFO, so a wrong mount is visible in one
  line. An unusable socket directory exits 3 with the uid, the mode and what to change.
- **Model lifecycle:** every load logs the id, the duration and what it evicted
  (`loaded manga-ocr in 2375.0ms (unloaded rapidocr-ppocrv5)`). Every unload logs. This is
  the audit trail for the one-model invariant.
- **Fetching:** each file logs its URL on download and its digest prefix on verification. A
  digest mismatch logs the expected and actual values at WARNING before re-downloading.
- **Rejections:** a refused connection, a stalled peer and an unsendable response each log at
  WARNING with the reason.
- **Per-request timing** rides in the response rather than the log: `load_ms` and `infer_ms`.
  The orchestrator is the natural place to aggregate them, since it already has the request
  context and a metrics surface.

What to watch first, in the absence of a metrics stack: `readyz` flipping false (the worker
is telling you it cannot work and why, in `reasons`), and repeated `busy` responses (the
orchestrator is opening connections faster than it closes them).

Deliberately not shipped: a Prometheus endpoint. That would be an HTTP listener, and the
argument in Alternatives applies. When metrics are wanted, the orchestrator should export
them from the values it already receives.

## Security and privacy

- **The repository is public.** No secrets anywhere in the service, and none needed: the
  worker authenticates to nothing. Hugging Face is fetched anonymously.
- **Data touched:** the images the user submits, in memory only. Nothing is written to disk
  except model weights, and no recognised text is logged — only counts and timings. An OCR
  payload can contain anything the user photographed, so it must stay out of the logs.
- **Access control is the filesystem.** The socket is mode `0660` in a directory shared only
  with the orchestrator. There is no network listener at all, so there is no port to firewall
  and no token to rotate or leak.
- **Supply chain.** Every model file is pinned to an immutable commit sha and a sha256. A
  silently changed upstream becomes a loud `checksum_mismatch`, and an unverifiable file is
  unloadable. `HF_ENDPOINT` is restricted to http and https, so it cannot be redirected at
  `file://`. Downloads are cut off at the pinned size while streaming.
- **Blast radius.** The container runs as a non-root user, with `mem_limit: 2g` and
  `cpus: 1.0`, and holds no credentials. A compromised worker can read the images sent to it
  and the model cache, and nothing else.
- **Untrusted input.** Image decoding happens in Pillow and OpenCV, which is the largest
  attack surface in the process. The payload cap is 64 MiB. This is the part most worth
  keeping patched; the lockfile and Dependabot exist partly for that.

## Testing

Three levels, and they exist for different reasons. The shorthand is: the tables say the
logic is right, the socket tests say the process is right, the end-to-end run says the
model is right.

**Level 1 — pure unit tables.** No sockets, no threads, no files, no network. Every one is
a `subTest` table: one row per case, so a failure names the row and adding a case is one
line. This is where the OCR logic lives, and it exists because for a while it did not: the
three engine adapters sat at 0% coverage while the plumbing around them was tested hard, so
a regression in TSV parsing or the decode loop would have passed the whole suite.

| Table | What it pins |
| --- | --- |
| Tesseract TSV | Folding one-row-per-word output into lines: geometry, the union box, mean confidence, blank and negative-confidence rows dropped, malformed rows skipped, block and line grouping, Japanese joined without spaces. Plus one captured real page as an anchor. |
| Tesseract shell-out | The argv we build, options reaching the command line, and every refusal: no binary, missing language data, `--list-langs` failing, a non-zero exit carrying its stderr. `subprocess.run` is the seam; no tesseract is installed or used. |
| manga-ocr decode | The greedy loop, which is entirely ours: the EOS stop, the max-token stop, the whole prefix being re-fed each step, special and out-of-range tokens dropped, ellipsis normalisation, the encoder called once with a normalised NCHW batch, and confidence as the mean of per-token softmax maxima. A scripted fake decoder drives it. |
| RapidOCR assembly | The library's parallel tuples into our line shape: order, newline joining, rounding of scores and boxes, nulls when the library omits boxes or scores, an empty result, and BGR channel order on the way in. |
| Engine factory | Each name building its adapter, `ENGINE_NAMES` not drifting from what is buildable, and an unknown engine naming the ones that exist. |
| Registry | Ten malformed manifests: id traversal, missing engine, unknown source type, unpinned revision, escaping `dest`, duplicate ids, a dangling `default_model`. |
| Fetcher refusals | Every way fetched bytes can fail to be the pinned bytes, each ending with nothing left on disk. |
| Probe lines | The exact string `--probe` prints, byte for byte, for pass and fail. |

**Level 2 — socket-level scenarios.** A real `SocketServer` on a real unix socket, with a
stub manager in place of engines. These are deliberately **not** tables: a stalled peer, the
connection cap, a cold load in flight, 24 concurrent loads and a failed fetch each need
their own threads, fixtures and teardown, and each fails in its own way. Forcing them into a
table would put a row of optional setup flags in the fixture and a pile of branches in the
loop, and the loop would become the thing under test. The test file says so in a comment, so
nobody "finishes the job" later.

They cover: a control block four times larger than `SO_SNDBUF` arriving intact, a peer that
announces a payload and stalls, a peer that closes mid-message, an oversized datagram, the
17th connection being refused, the probe ops over the real transport, and the one-model
invariant sampled from *inside* the critical section.

**Level 3 — end-to-end, by hand.** Needs the model files and the network, so it is not in
the suite: the Go reference client running `handshake → list → load → infer → unload`
against a live worker and printing real recognised text; a cold fetch into an empty models
directory with the digests compared against `models.yaml`; a corrupt cache with the source
unreachable; all three engines inside the container under the compose limits.

**Coverage.** `make coverage` runs the suite under `coverage` and enforces a floor. The
number is a tripwire, not a target — a test that asserts nothing raises it exactly as well
as a test that asserts something — so the table below is what matters, and the floor exists
only so the engines cannot silently go dark again.

**What is deliberately not covered, and why.**

- **The three engine constructors.** `RapidOcrEngine.__init__` opens two ONNX sessions
  through RapidOCR and `MangaOcrEngine.__init__` opens two more; both need real weights on
  disk. Mocking them would assert that our mock was called, not that the model loads, so
  they are left to the end-to-end run. This is most of the residual gap in those two files.
- **Anything needing the network.** The fetcher's HTTP path is exercised with `urlopen`
  patched; a real download is a level-3 check.
- **Model accuracy.** Nothing in the suite asserts that PP-OCRv5 reads Japanese correctly —
  that is a property of the weights, not of this code, and it is checked by the end-to-end
  run against known images.
- **`__main__`'s argument parsing and startup.** Covered by running the thing, not by unit
  tests; the probe path inside it is unit-tested because it has a printed contract.

**The scenario most likely to catch the next real bug** remains a dense page. It produced the
one confirmed defect so far — a 221 KB control block that `send` refused — and it is where
line count, response size and the detector's candidate ceiling all interact.

**Mutation-checked.** The engine tables were validated by breaking the code they cover, one
change at a time: shifting a TSV column, dropping the confidence filter, always space-joining,
grouping by block instead of line, breaking the EOS stop, feeding only the last token,
keeping special tokens, reading the first timestep instead of the last, dropping the rounding,
dropping the BGR conversion, joining lines with a space, and mis-mapping an engine name. All
twelve were caught, each by the specific row that should have caught it.

## Decisions

The open questions from the first draft, resolved by Dhira. Each says what was decided and
what it leaves to do.

**`infer` takes no `model` field. Settled.** The orchestrator must `load` first and `unload`
after, so which model answered a request is never in doubt. `infer` validates: with nothing
resident it returns `no_model_loaded` rather than guessing, and a control block carrying a
`model` field is now **refused** rather than silently ignored — sending one means the caller
has a different model in mind than the resident one, and running the resident one anyway
would answer the wrong question. The same check refuses any field an op does not take.

**No `cancel` op. Settled, with work attached.** Cancelling wastes the bandwidth already
spent. Two things follow, and neither exists yet:

- **Work must survive a client disconnect.** A dropped connection today loses the response;
  the inference itself continues to completion and the result is discarded. That is the
  right half. The missing half is that the result should remain retrievable.
- **A client must be able to reconnect and ask for current progress**, so a progress bar is
  possible. That needs a job identity, a progress field the engine updates as it works, and
  an op to query it. This is the next protocol change to design.

Until then a Go-side timeout abandons the *response*, not the work: the worker keeps going,
and the orchestrator must not assume a timeout freed the lock.

**manga-ocr's decode cost is not accepted.** The exported decoder has no key/value cache, so
each step re-runs it over the whole prefix — quadratic in output length, and an allocation
per step. **Open technical task:** re-export the decoder with past key/values (or an
equivalent reuse), and measure it. If that proves impossible, the orchestrator's queue has to
budget for the real cost rather than the nominal one. Not settled either way; it needs the
export attempt first.

**Shared code across `-stt` and `-tts` goes to `packages/`.** The convention, verified
against uv: a service depends on a workspace member by name and declares
`[tool.uv.sources] <name> = { workspace = true }`. It resolves into the same root `uv.lock`
as `source = { editable = "packages/<name>" }` — no version pinning, and edits are live
because it is installed editable. Services themselves stay `package = false` and appear as
`virtual`; only real shared packages are installable. **The extraction is a follow-up**: the
protocol, the registry and the fetcher are the obvious candidates, but nothing moves until a
second worker exists to share them with, because one caller is not yet a pattern.

## Open questions

- [ ] **Q:** Where do metrics go, given there is deliberately no HTTP endpoint here? Dhira
  has asked for a recommendation; none is offered yet, so this stays open. — *owner:* Dhira
  — *needed by:* the first dashboard.
