# inferences-ocr

The OCR inference worker. A long-lived Python process that holds **exactly one** OCR model
in memory and answers requests over a unix socket.

It does tensors and nothing else. The Go orchestrator (`services/dita-orchestrator`) owns
the client-facing HTTP, the request queue, the resource budget, and the decision about
*which* model should be resident. This service just does what it is told, and says clearly
when it cannot.

- **Engine policy:** ONNXRuntime only. No torch, no Paddle, no CUDA.
- **Weights policy:** never committed, never baked into the image. `models.yaml` pins them;
  the fetcher downloads them into the mounted `$MODELS_DIR` and verifies every sha256.
- **Residency policy:** one model at a time. Loading B unloads A first.

## The model registry

`models.yaml` is the single source of truth for what is selectable. Three models ship with it:

| id | engine | langs | notes |
| --- | --- | --- | --- |
| `rapidocr-ppocrv5` | `rapidocr` | ja, en, zh | **The default.** PaddleOCR PP-OCRv5 mobile detection + recognition as ONNX, driven by RapidOCR. ~21 MB of weights. |
| `tesseract` | `tesseract` | ja, en | The system binary. Nothing is downloaded; the image installs `tesseract-ocr` plus the `eng`, `jpn` and `jpn-vert` language data. |
| `manga-ocr` | `manga_ocr` | ja | Japanese vertical / manga text specialist, run as two ONNX graphs. ~460 MB of weights. Reads one text block per call, so feed it a crop rather than a page. |

Every `sha256` and `bytes` in `models.yaml` was computed from a real download, not guessed.
The header comment in that file says so explicitly and says what to do when a digest cannot
be verified: write `sha256: null`, and the fetcher will refuse to load the model rather than
accept unknown bytes.

### How to add a model

1. **Find the files.** You need an immutable Hugging Face commit sha, not a branch name.
   `curl -s https://huggingface.co/api/models/<repo> | jq -r .sha` gives you one.
2. **Get the real digests.** Download each file once and hash it:
   ```bash
   curl -sL "https://huggingface.co/<repo>/resolve/<sha>/<path>" -o /tmp/f
   sha256sum /tmp/f && stat -c %s /tmp/f
   ```
   For LFS-tracked files you can cross-check against `lfs.oid` in
   `https://huggingface.co/api/models/<repo>/tree/main?recursive=true`, which is the sha256.
3. **Add the entry** to `models.yaml`: `id`, `description`, `engine`, `langs`, `source`,
   `files[]`, `options`. A file may override `repo`/`revision` when one logical model is
   assembled from more than one upstream repo, which is how `rapidocr-ppocrv5` pulls its
   detector and its recogniser from two different PaddlePaddle repos.
4. **Pick an engine.** If it is one of `rapidocr`, `tesseract`, `manga_ocr`, you are done.
   Otherwise add an adapter under `ocr_worker/engines/`: subclass `Engine`, implement
   `infer(image_bytes) -> Result`, and wire the name into `ocr_worker/engines/__init__.py`.
5. **Run the tests** (`python -m unittest discover -s tests -t .`) — they assert that every
   downloadable file in the registry carries a 64-character digest.

## Running it

### With compose (how it actually runs)

```bash
docker compose -f deployment/docker-compose.yml up --build inferences-ocr
```

The socket lands on the shared `dita-sockets` volume at `/run/dita/inferences-ocr.sock`;
weights are cached on the `ocr-models` volume. The container runs as the non-root `ocr`
user and starts with **no model loaded** — the orchestrator decides what to load.

### Locally

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
MODELS_DIR=./models SOCKET_PATH=/tmp/ocr.sock .venv/bin/python -m ocr_worker
```

Useful flags: `--preload <model id>` to load something at startup, `--log-level debug`,
`--registry` to point at a different `models.yaml`.

### Dev HTTP mode

Off unless you ask for it. For poking at the worker by hand, never for production:

```bash
python -m ocr_worker --http 127.0.0.1:8099
curl -s localhost:8099/handshake
curl -s localhost:8099/list
curl -s -X POST localhost:8099/load -d '{"id":"rapidocr-ppocrv5"}'
curl -s -X POST localhost:8099/infer --data-binary @page.png
```

## The wire protocol

**Transport:** `AF_UNIX` / `SOCK_SEQPACKET`, bound at `$SOCKET_PATH` with mode `0660`.
Protocol version 2.

**Framing.** SEQPACKET preserves message boundaries and ordering, so a message needs no
length prefix — only a count of the bytes that follow it. A message has three parts:

```
datagram 0     : the prologue, a small fixed-shape JSON object:
                 {"protocol": 2, "control_len": N, "payload_len": M}
next datagrams : the control block, in chunks of at most 65536 bytes
next datagrams : the payload, in chunks of at most 65536 bytes
```

A length of 0 means that section sends no datagrams at all. No conforming datagram is ever
empty, so an empty read means the peer closed.

**Both sections are chunked, and this matters.** A single AF_UNIX datagram cannot exceed
`SO_SNDBUF` — 212992 bytes on a default Linux kernel. Above that, `send` fails with
`EMSGSIZE`; it does not fragment, and a larger datagram cannot be sent at all. A dense page
produces thousands of lines, and an `infer` response runs about 120 bytes per line, so the
control block reaches that ceiling as readily as an image does. Chunking both is what keeps
a 4000-line result sendable.

The receive buffer is exactly one chunk. A peer that sends a larger datagram is caught by
`MSG_TRUNC` and answered with `bad_request`, rather than being silently truncated.

**Limits**, all advertised in the `handshake` response under `limits` so a client never has
to hard-code them:

| field | value | meaning |
| --- | --- | --- |
| `max_chunk` | 65536 | largest datagram either side may send |
| `max_control` | 8 MiB | largest control block, roughly 70000 OCR lines |
| `max_payload` | 64 MiB | largest image |
| `idle_timeout_s` | 300 | how long an open connection may sit between messages |
| `message_timeout_s` | 30 | how long a half-sent message may stall before it is dropped |

The server accepts at most 16 concurrent connections. Beyond that it answers `busy` and
closes, rather than spawning unbounded threads that each hold buffered chunks.

**Ops.**

| op | request fields | response |
| --- | --- | --- |
| `handshake` / `version` | — | `service`, `version`, `protocol`, `limits`, `engines[]`, `default_model`, `resident`, `loading` |
| `list` | — | `models[]` (id, description, engine, langs, source_type, files, bytes, unverified_files), `default_model`, `resident`, `loading` |
| `load` | `id` | `id`, `engine`, `already_resident`, `load_ms`, `unloaded` (the model that was evicted, or null — always present) |
| `unload` | — | `unloaded` (id or null) |
| `infer` | payload = encoded image bytes (PNG/JPEG/...) | `text`, `lines[]`, `model`, `infer_ms` |

Every response carries `ok`. A failure is `{"ok": false, "error": {"code", "message"}}` with
one of: `bad_request`, `unknown_model`, `unsupported_engine`, `checksum_mismatch`,
`fetch_failed`, `no_model_loaded`, `timeout`, `busy`, `response_too_large`, `internal`.

`resident` is the model that can answer `infer` right now. `loading` is the id of a model
being fetched, if any — a load downloads before it evicts, so during a fetch `resident` is
still the previous model and it still serves requests.

**The infer result.**

```json
{
  "ok": true,
  "text": "Hello dita OCR 2026\nThe quick brown fox\n日本語のテキスト認識",
  "lines": [
    {"text": "Hello dita OCR 2026", "confidence": 0.96812,
     "box": [[37.0, 41.0], [509.0, 40.0], [510.0, 90.0], [37.0, 91.0]]}
  ],
  "model": "rapidocr-ppocrv5",
  "infer_ms": 966.2
}
```

`text` is the lines joined with newlines. `box` is four `[x, y]` corners in source-image
pixels, clockwise from top-left; it is `null` for engines that do not localise text
(`manga-ocr` returns one line and no box). `confidence` is 0..1, or `null` when the engine
does not report one.

**Loading is not implicit.** `infer` with nothing resident returns `no_model_loaded` rather
than quietly loading the default. Choosing what to load is the orchestrator's job, and a
load can mean a multi-hundred-megabyte download.

## How Go integrates

The orchestrator speaks this same socket. Nothing in this service is HTTP-shaped, and the
dev HTTP mode is not part of the contract.

- **Dial:** `net.Dial("unixpacket", "/run/dita/inferences-ocr.sock")`. Go's `unixpacket`
  network *is* `SOCK_SEQPACKET`, so the framing above maps directly: one `Write` per
  datagram, one `Read` per datagram, with a read buffer of `max_chunk`. Read the prologue,
  then read `control_len` bytes worth of datagrams, then `payload_len` bytes worth. Do not
  wrap the connection in `bufio` — that would destroy the message boundaries the protocol
  relies on. **Never write more than `max_chunk` in one call**; the kernel rejects the whole
  datagram rather than splitting it.
- **Handshake once per connection** and check `protocol`. A worker that answers a protocol
  number you do not know is a worker you should not drive. Take `max_chunk` and the two
  timeouts from `limits` instead of hard-coding them.
- **Set deadlines.** The worker drops a connection that stalls mid-message after
  `message_timeout_s` and an idle one after `idle_timeout_s`, answering `timeout` first
  where it still can. Mirror those with `SetDeadline` on the Go side.
- **The orchestrator owns the queue.** This worker holds one exclusive lock, so concurrent
  `infer` calls serialise behind it; it will not reject or shed load, except past 16
  concurrent connections where it answers `busy`. Rate limiting, timeouts, retries and
  backpressure belong on the Go side.
- **The orchestrator owns model choice.** Call `list` to see what is selectable, `load` to
  select. `load` always returns `unloaded`, so the orchestrator knows what it evicted.
  Because a load can be slow (download + session init), treat it as a control-plane
  operation, not something to do per request. A request whose model is not resident should
  either queue behind one `load` or be answered from the resident model, whichever the
  policy says — the worker will not make that decision.
- **A load downloads before it evicts.** While a fetch is running, `loading` names the
  incoming model and `resident` still names the outgoing one, which keeps answering `infer`.
  A failed fetch therefore leaves the working model in place; only a failure to *construct*
  the new engine, after the fetch succeeded, leaves nothing resident.
- **The one-model invariant is enforced here, not there.** Asking for B while A is resident
  always evicts A. There is no way to get two models loaded, so the Go side can budget memory
  as "the largest single model", not "the sum".
- **Failure handling:** `error.code` is the stable part of an error; `error.message` is for
  humans and logs. `checksum_mismatch` and `fetch_failed` mean the model is unusable and a
  retry of `infer` will not help — retry the `load`, or pick a different model.

## Deviations worth knowing

- **PP-OCRv5 recognition keys.** The PaddlePaddle ONNX export carries no `character`
  metadata, which is where RapidOCR normally reads the CTC label set from. On first load the
  worker writes `rec_keys.txt` next to the model, taking the labels from
  `PostProcess.character_dict` in the pinned (and checksummed) `inference.yml`.
- **Angle classification is off** for `rapidocr-ppocrv5` (`use_cls: false`) — one fewer model
  file, and RapidOCR's vertical padding handles the common cases. Turn it on in `options` and
  add the cls model to `files[]` if rotated-180 text starts mattering.
- **manga-ocr skips jaconv.** Upstream runs a half-width to full-width conversion over its
  output; this port does the whitespace and ellipsis normalisation but not that conversion,
  to keep the dependency set small.
- **Tesseract line assembly.** Tesseract emits one TSV row per word. The adapter folds rows
  back into lines and joins Japanese runs without spaces, since spacing them would corrupt
  the text.

## Tests

```bash
cd services/inferences-ocr && python -m unittest discover -s tests -t .
```

Stdlib only, no network, no weights. 36 tests covering:

- **Framing** — a control block four times larger than `SO_SNDBUF`, a peer that announces a
  payload and then stalls, a peer that closes mid-message, an oversized datagram, and
  lengths past the ceilings.
- **The real server over a real socket** — the oversized response arriving intact, a stalled
  peer getting `timeout` rather than a reset, and the 17th connection getting `busy`.
- **The registry** — parsing, id validation against path traversal, and the rule that every
  downloadable file carries a digest.
- **The fetcher** — a bad digest, a corrupt cache, an oversized download, a hostile
  `HF_ENDPOINT` scheme, and that a verified file is not re-hashed on the next load.
- **The one-model invariant** — sampled from *inside* the critical section, under 24
  concurrent loads, under a failed fetch, and while a download is in flight.
- **`dispatch`** — handshake, list, load, unload and the error paths.
