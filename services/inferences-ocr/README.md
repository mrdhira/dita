# inferences-ocr

> One OCR model in memory, one unix socket, and nothing else. The Go orchestrator decides what to load; this service does tensors.

**Status: v0, in review.** First inference worker in the dita monorepo, and the reference
shape for `inferences-stt` and `inferences-tts`. The wire protocol is at version 2 and can
still move before the orchestrator client ships.

## TL;DR

```bash
docker compose -f deployment/docker-compose.yml up --build inferences-ocr
```

- **Japanese and English** from one model: PaddleOCR PP-OCRv5 mobile, as ONNX, driven by RapidOCR
- **ONNXRuntime only.** No torch, no Paddle, no CUDA — `pip list | grep -i torch` in the image returns nothing
- **One model resident, ever.** `load` evicts whatever was there and tells you what it evicted, so memory budgets are "the largest model", not "the sum"
- **Weights are never committed and never baked into the image.** `models.yaml` pins every file to an immutable upstream revision with a sha256; the fetcher refuses anything else
- **Not HTTP.** `AF_UNIX` `SOCK_SEQPACKET`, same kernel, no TCP stack and no HTTP parser — see [Why](#why)
- **Three models selectable:** PP-OCRv5 (default), the system tesseract binary, and manga-ocr for Japanese vertical text
- Health probes are protocol ops, not URLs: `livez`, `readyz`, `startupz`

## What is this?

A long-lived Python process that holds exactly one OCR model and answers requests over a unix
socket. It owns model residency and inference. It does not own the queue, the retry policy,
the resource budget, or the decision about which model should be loaded — all of that belongs
to `services/dita-orchestrator`, which is the only thing that talks to it.

### The registry

`models.yaml` is the single source of truth for what is selectable.

| id | engine | langs | weights | notes |
| --- | --- | --- | --- | --- |
| `rapidocr-ppocrv5` | `rapidocr` | ja, en, zh | 21 MB | **The default.** PP-OCRv5 mobile detection + recognition as ONNX. |
| `tesseract` | `tesseract` | ja, en | none | The system binary; the image installs `eng`, `jpn` and `jpn-vert`. A baseline, not a recommendation. |
| `manga-ocr` | `manga_ocr` | ja | 460 MB | Japanese vertical and manga text. Reads one block per call, so feed it a crop. |

Every `sha256` and `bytes` in that file was computed from a real download, not guessed. When
a digest cannot be verified the rule is to write `sha256: null`, and the fetcher then refuses
to load the model rather than accept unknown bytes.

### Who owns which part of the pipeline

The three adapters look very different, and that is a boundary difference rather than an
inconsistency. Each engine hands us a different amount of the work already done.

| Engine | What the upstream owns | What this service owns |
| --- | --- | --- |
| **RapidOCR (PP-OCRv5)** | everything: resize, DBNet detection, box unclipping, CRNN recognition, CTC decode with its softmax and blank-collapsing, confidence averaging | almost nothing — pick the pinned ONNX files, supply the label set, choose BGR order, reshape the result |
| **Tesseract** | everything: binarisation, layout analysis, recognition, its own confidences | fold its one-row-per-word TSV back into lines; normalise nothing |
| **manga-ocr** | nothing — it ships a bare encoder graph, a bare decoder graph and a vocabulary | all of it: greyscale, resize, normalise, run the encoder, greedy-decode the decoder with an EOS stop, softmax the winning logit into a confidence, map ids back through the vocabulary, assemble the line |

The softmax in the manga-ocr loop deserves a word, since RapidOCR has no equivalent in our
code. `argmax` picks the next token straight from the logits and needs no normalisation; the
softmax exists **only** to turn that winning logit into a probability, so the per-token
numbers are comparable and their mean is a meaningful line confidence. Without it the field
would carry a raw logit, which is not on any scale a caller can reason about. RapidOCR does
the same thing inside its own CTC decoder — we just never see it.

Each adapter's module docstring says this again in more detail, next to the code it governs.

## How to use it

### With compose

```bash
docker compose -f deployment/docker-compose.yml up --build inferences-ocr
```

The socket appears at `run/inferences-ocr.sock` in the repo and the weights land in
`services/inferences-ocr/models/`. Both are bind mounts, so they can be inspected with `ls`,
and `rm -rf services/inferences-ocr/models/*` is a complete reset. Their contents are
gitignored; the directories themselves are kept by a `.gitkeep`.

The container starts with **no model loaded** — that is the orchestrator's call.

### Locally

```bash
uv sync --package inferences-ocr        # from the repo root; creates .venv from uv.lock
MODELS_DIR=./models SOCKET_PATH=../../run/inferences-ocr.sock \
    uv run --package inferences-ocr python -m ocr_worker
```

Dependencies are declared in `services/inferences-ocr/pyproject.toml` and resolved in the
single `uv.lock` at the repo root. There is no `requirements.txt`: the image build generates
a pinned, hash-carrying export from the lock and installs that.

Useful flags: `--preload <model id>`, `--log-level debug`, `--registry` for a different
`models.yaml`, `--probe live|ready|startup` for a one-shot health check.

### From Python

There is no HTTP debug mode. This is the whole client:

```python
import json, socket

MAX_CHUNK = 64 * 1024

def call(sock, op, payload=b"", **fields):
    control = json.dumps({**fields, "op": op}).encode()
    sock.send(json.dumps({"protocol": 2, "control_len": len(control),
                          "payload_len": len(payload)}).encode())
    for blob in (control, payload):
        for i in range(0, len(blob), MAX_CHUNK):
            sock.send(blob[i:i + MAX_CHUNK])

    head = json.loads(sock.recv(MAX_CHUNK))
    body = b""
    while len(body) < head["control_len"]:
        body += sock.recv(MAX_CHUNK)
    return json.loads(body)

s = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
s.connect("../../run/inferences-ocr.sock")
print(call(s, "list")["models"])
print(call(s, "load", id="rapidocr-ppocrv5"))
print(call(s, "infer", open("page.png", "rb").read())["text"])
```

### From Go

A complete, stdlib-only reference client lives in
[`examples/go/`](examples/go/) — run it with `go run . -image page.png`. That is the shape the
orchestrator will follow.

### The wire protocol

**Transport:** `AF_UNIX` / `SOCK_SEQPACKET` at `$SOCKET_PATH`, mode `0660`. Protocol 2.

**Framing.** SEQPACKET preserves message boundaries, so a message carries no length prefix,
only a count of the bytes that follow:

```
datagram 0     : the prologue — {"protocol": 2, "control_len": N, "payload_len": M}
next datagrams : the control block, in chunks of at most 65536 bytes
next datagrams : the payload, in chunks of at most 65536 bytes
```

Both sections are chunked, and that matters: an AF_UNIX datagram cannot exceed `SO_SNDBUF`
(212992 bytes by default), above which `send` fails with `EMSGSIZE` rather than fragmenting.
A dense page's result runs past that ceiling as readily as an image does. The receive buffer
is exactly one chunk, and a larger datagram is caught by `MSG_TRUNC` rather than silently cut.

**Ops.**

| op | request | response |
| --- | --- | --- |
| `handshake` / `version` | — | `service`, `version`, `protocol`, `limits`, `engines[]`, `ops[]`, `default_model`, `resident`, `loading` |
| `list` | — | `models[]`, `default_model`, `resident`, `loading` |
| `load` | `id` | `id`, `engine`, `already_resident`, `load_ms`, `unloaded` (always present) |
| `unload` | — | `unloaded` |
| `infer` | payload = encoded image bytes, no fields | `text`, `lines[]`, `model`, `infer_ms` |
| `livez` | — | `probe`, `status`, `uptime_s`, `reasons[]` |
| `readyz` | — | the same, plus `resident` and `loading` |
| `startupz` | — | `probe`, `status`, `uptime_s`, `reasons[]` |

**Limits**, all advertised under `limits` in the handshake so nothing is hard-coded:
`max_chunk` 65536, `max_control` 8 MiB, `max_payload` 64 MiB, `idle_timeout_s` 300,
`message_timeout_s` 30. At most 16 concurrent connections; past that the answer is `busy`.

Every response carries `ok`. A failure is `{"ok": false, "error": {"code", "message"}}` with
one of `bad_request`, `unknown_model`, `unsupported_engine`, `checksum_mismatch`,
`fetch_failed`, `no_model_loaded`, `timeout`, `busy`, `response_too_large`, `internal`.

An `infer` result:

```json
{"ok": true,
 "text": "Hello dita OCR 2026\nThe quick brown fox\n日本語のテキスト認識",
 "lines": [{"text": "Hello dita OCR 2026", "confidence": 0.96812,
            "box": [[37.0, 41.0], [509.0, 40.0], [510.0, 90.0], [37.0, 91.0]]}],
 "model": "rapidocr-ppocrv5", "infer_ms": 966.2}
```

`box` is four `[x, y]` corners in source pixels, clockwise from top-left, or `null` for
engines that do not localise text. `confidence` is 0..1, or `null`.

`infer` with nothing resident returns `no_model_loaded`. It does not quietly load the
default: choosing what is resident is the orchestrator's job, and a load can mean a
multi-hundred-megabyte download. It also takes **no `model` field** — send one and it is
refused, because a caller naming a model wants that model, and running the resident one
instead would answer a different question. `load` first, `unload` after. Any field an op
does not take is refused the same way rather than ignored.

### Health probes

Three ops, named after the Kubernetes convention so the semantics are recognisable.
`/healthz` has been deprecated since Kubernetes v1.16 and is deliberately not offered.

| probe | true when | false means |
| --- | --- | --- |
| `livez` | the process and its accept loop are up; no dependency checks | **restart me** |
| `readyz` | socket bound, registry parsed, models dir writable, and either something is resident or a load could still proceed | **stop routing to me** |
| `startupz` | the one-time boot work finished: socket bound, registry parsed | **do not kill me, I am still booting** |

A container healthcheck is an exec probe, which is the standard for a service with no HTTP
surface:

```bash
python -m ocr_worker --probe ready   # exits 0 on pass, 1 on fail
```

Two things worth knowing. A cold load does **not** make `readyz` false — a download in flight
is progress, not a wedge — so a first `load` of manga-ocr's 460 MB cannot trip the
healthcheck. And `readyz` answers "can I be given work", including a `load`; the `resident`
field in its response is what tells you whether an `infer` would succeed this instant.

### How Go integrates

- **Dial** `net.Dial("unixpacket", "/run/dita/inferences-ocr.sock")`. Go's `unixpacket` *is*
  `SOCK_SEQPACKET`, so the framing maps directly: one `Write` per datagram, one `Read` per
  datagram, read buffer of `max_chunk`. No `bufio` — it would destroy the boundaries.
- **Handshake once per connection** and check `protocol`. Take the limits and timeouts from
  the response instead of hard-coding them.
- **Set deadlines** to match `idle_timeout_s` and `message_timeout_s`.
- **The orchestrator owns the queue.** The worker holds one exclusive lock, so concurrent
  `infer` calls serialise; it never sheds load except past 16 connections.
- **The orchestrator owns model choice.** A load downloads *before* it evicts, so during a
  fetch `loading` names the incoming model while `resident` still names the outgoing one and
  keeps serving. A failed fetch leaves the working model in place.

## Why

**Why not HTTP.** The orchestrator and the worker are on the same host, in the same pod, on
the same kernel. An HTTP surface would mean a TCP listener, an HTTP parser, a framing
library, and a server dependency such as FastAPI or uvicorn — all to move bytes between two
processes that could pass them through a socket the kernel already owns. It would also mean
a second transport to secure and to keep in sync. `AF_UNIX` `SOCK_SEQPACKET` gives message
boundaries for free, which is most of what a framing layer does, and filesystem permissions
give access control without a token. The trade is that a client has to implement about forty
lines of framing; `examples/go/` is that, in the standard library, once.

The same argument retired the dev-only HTTP mode this service used to carry: it duplicated
the surface, it had its own bugs, and the Python snippet above replaces it.

**Why one model at a time.** Several models are *selectable*, never co-resident. That turns a
memory budget into a single number — the largest model, not the sum — which is what makes
`mem_limit: 2g` a promise rather than a hope. It also means a load is a control-plane event
with a visible cost, so the orchestrator schedules it rather than stumbling into it.

**Why pinned digests.** Model weights come from someone else's repository. Pinning a commit
sha and a sha256 makes a silent upstream change into a loud failure, and `sha256: null` makes
an unverified model unloadable instead of quietly trusted.

**Why not tesseract as the default.** It is a system C++ binary and a genuinely useful
baseline, but on Japanese it is not close. For an image reading `日本語のテキスト認識` it
returns `AA 告 の テキ ス ト 認識`, where PP-OCRv5 is exact. It stays in the registry as a
cheap fallback. (`pytesseract` would add a dependency for nothing: it shells out to the same
binary and re-parses the same TSV we already parse for boxes.)

## Cost

Read it as a price list. Two columns, because the gap between them is the point: the
container is capped at `cpus: 1.0`, and OCR is CPU-bound, so the limit is most of the price.
x86-64 Linux, PP-OCRv5 mobile under ONNXRuntime CPU, weights already fetched unless stated.

| Situation | Host, uncapped | Container, `cpus: 1.0` |
| --- | --- | --- |
| `load` — cold, downloads 21 MB and verifies four digests | **6.1 s** | — |
| `load` — warm, ONNX sessions only | **0.3–1.3 s** | **3.6 s** |
| `infer` — 900×300 page, 3 lines | **1.0–1.6 s** | **7.6 s** |
| `infer` — 3600×4400 page, 900 lines, 221 KB response | **33.9 s** | **359.6 s** |
| `load` of `manga-ocr` — cold, 460 MB | — | **16.6 s** |
| `load` of `manga-ocr` — warm | **2.4–5.7 s** | — |

Ranges are run-to-run spread on an otherwise busy machine, not error bars; treat them as
orders of magnitude rather than benchmarks.

What to take from it:

- **Load latency is not in the request path.** Once a model is resident, `load` costs
  nothing — the steady-state number is the `infer` row.
- **One core is the constraint, not memory.** Give the container two cores before giving it
  more RAM.
- **A dense page is a different workload.** 900 lines is 900 recognition passes, and it
  scales with line count, not page area. If that becomes a real input, batch it or raise the
  CPU allocation; do not expect the 3-line number.

Memory and size:

| | |
| --- | --- |
| Resident, PP-OCRv5 | ~140 MB, plus ~100 MB transient during an inference |
| Peak RSS with manga-ocr resident | ~790 MB — the reason `mem_limit` is 2g |
| Image | 1.21 GB, mostly opencv's GL dependencies and tesseract's language data |

## Contributions

```bash
# build
make build           # docker compose build, context = repo root

# test — stdlib only, no network, no weights; uv syncs from the lock first
make test
make coverage        # the same suite under coverage, with a floor

# dependencies
make lock            # re-resolve uv.lock after editing a pyproject.toml
make lock-check      # fail if uv.lock is stale — the same assertion the build makes
make lock-upgrade    # re-resolve within the declared bounds
```

### How dependencies are managed

This service is a member of a **uv workspace** rooted at the repo. The root
`pyproject.toml` is virtual — no `[project]` table, just `[tool.uv.workspace]` — and the
whole monorepo resolves into one `uv.lock` beside it. Bounds live in each service's
`pyproject.toml`; exact versions and hashes live in the lock.

**The build context is the repo root**, not this directory, because the lock lives at the
top and a Docker build context cannot reach outside itself. That is the only reason; the
Dockerfile still copies nothing but this service's manifest, models registry and source.

**Dependabot is the only update mechanism.** A weekly workflow used to run alongside it,
because Dependabot reading a flat `requirements.txt` could not see transitive packages —
`antlr4-python3-runtime`, pinned exactly by omegaconf, which rapidocr pulls in, was the
case that proved it. With a lockfile that gap closes: Dependabot's `uv` ecosystem reads the
root manifest plus `uv.lock` and updates packages inside the lock, transitive ones included.
One tool, one grouped PR a week.

**The guard that replaces the workflow** is in the Dockerfile: it exports the requirements
with `uv export --locked`, which verifies the lock against the manifests. Edit a bound
without running `make lock` and the image build fails. Note that `--frozen` is *not* the
flag for this — it refuses to update the lock but does not check it, and will export a stale
one and exit 0.

66 tests at two levels, described in full in the
[technical requirement](../../docs/inferences/ocr/%5B1%5Dtechnical-requirement.md#testing).

**Tables** (`subTest`, one row per case) for everything pure: the Tesseract TSV fold and the
command we build for it, the manga-ocr decode loop, the RapidOCR result assembly, the engine
factory, registry validation, fetcher refusals, and the exact line `--probe` prints. These
are the OCR logic, and they were added because the three engine adapters had been at 0%
coverage while the plumbing around them was tested hard.

**Scenarios** (plain methods, deliberately not tables) for everything stateful: a stalled
peer, the connection cap, a cold load in flight, concurrent loads, a failed fetch. Each
needs its own threads and teardown and fails in its own way; a table would hide that.

The engine tables were validated by mutation — twelve deliberate breakages of the code they
cover, all twelve caught by the specific row that should catch them.

Not covered on purpose: the three engine constructors, which open real ONNX sessions and
need weights on disk; anything requiring the network; and model accuracy, which is a
property of the weights and is checked by the end-to-end run instead.

There is no linter configured yet; `.claude/rules/PYTHON-CODE-GUIDELINES.md` is still empty,
so house style here is "match the file you are in".

### Adding a model

1. Get an immutable commit sha: `curl -s https://huggingface.co/api/models/<repo> | jq -r .sha`.
2. Download each file once and hash it: `sha256sum` and `stat -c %s`. For LFS files, cross-check
   against `lfs.oid` in `https://huggingface.co/api/models/<repo>/tree/main?recursive=true`.
3. Add the entry to `models.yaml`. A file may override `repo`/`revision`, which is how
   `rapidocr-ppocrv5` assembles its detector and recogniser from two different repos.
4. If the engine is not `rapidocr`, `tesseract` or `manga_ocr`, add an adapter under
   `ocr_worker/engines/`: subclass `Engine`, implement `infer`, register the name.
5. Run the tests — they assert every downloadable file carries a 64-character digest.

## Suggestions

Things this service does not do, in rough order of when they will start to hurt.

- **No fd-passing or shared memory.** Every image is copied through the socket in 64 KiB
  chunks. Fine at 1.2 s per page; worth revisiting if batch throughput ever matters.
- **No cancel op.** A `load` in flight cannot be aborted. A Go-side timeout abandons the
  response, not the download.
- **manga-ocr re-runs the decoder over the whole prefix each step**, because the exported
  graph has no key/value cache. That is quadratic in output length. Re-export with a cache if
  it becomes the hot path.
- **manga-ocr skips jaconv** half-width to full-width conversion, which upstream applies.
- **Angle classification is off** for PP-OCRv5 (`use_cls: false`): one fewer file for a case
  our inputs do not have. Turn it on in `options` and add the cls model to `files[]` if
  rotated text starts appearing.
- **A failed engine *build* leaves nothing resident**, deliberately. The fetch happens first,
  so a network failure is harmless, but once the swap starts the old engine is released
  before the new one is constructed. Keeping both alive would break the one-model invariant.
