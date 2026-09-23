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
| [`services/inferences-embedding`](services/inferences-embedding/README.md) | Python | Text embeddings: nomic-embed-text-v1.5 (en, default), EmbeddingGemma and Qwen3-Embedding (multilingual). DIP for lifecycle, TEI's `/embed` for vectors. | in review |
| `services/inferences-stt` | Python | Speech to text. | placeholder |
| `services/inferences-tts` | Python | Text to speech. | placeholder |

Shared packages live under `packages/`. Deployment lives in
[`deployment/docker-compose.yml`](deployment/docker-compose.yml).

## Getting set up

Linux and macOS. Toolchain versions are pinned in [`.tool-versions`](.tool-versions) and
installed with [asdf](https://asdf-vm.com), which works the same on both:

```bash
asdf plugin add golang && asdf plugin add python && asdf plugin add uv
asdf install                      # reads .tool-versions
```

On macOS, Homebrew is a fine substitute for any of them (`brew install uv`, and
`brew install --cask docker` for Docker Desktop). Docker on Linux follows
[the upstream instructions](https://docs.docker.com/engine/install/).

Then ask the repo whether the machine is ready:

```bash
make doctor
```

It checks every tool and its version, that the Docker daemon is reachable, and that
`uv.lock` is in sync. Each failure prints the exact install command for the OS you are on,
and it exits non-zero, so it also works as a CI gate.

Python is the one tool asdf does not have to provide: uv provisions the interpreter for the
workspace, so `doctor` asks uv what this repo would run rather than reading whatever
`python3` happens to be first on your PATH. If it is missing,
`uv python install $(cat .python-version)`.

**The interpreter is pinned to an exact patch**, in [`.python-version`](.python-version),
and `doctor` asserts that exact version rather than the minor. A rolling tag changes what
you ran without changing anything you wrote. The same discipline applies to Go: every
`go.mod` carries `go` at the pinned patch and [`go.work`](go.work) carries that plus a
matching `toolchain` directive, which `doctor` also checks.

```bash
make test        # every service's tests
make coverage    # every service's coverage, reported per service
make build       # every service image
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
  timeouts, so no client hard-codes them. A field an op does not declare is refused, not
  ignored.
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
  is an exec probe rather than an HTTP one; each worker's README gives its command.

A stdlib-only Go reference client lives in
[`services/inferences-ocr/examples/go`](services/inferences-ocr/examples/go/) and is the
shape the orchestrator's client will take.

## Docs

Design documents live under `docs/`, one directory per service:

- [`docs/inferences/ocr/[1]technical-requirement.md`](docs/inferences/ocr/%5B1%5Dtechnical-requirement.md)
  — the OCR worker: context, the wire protocol, the per-engine pipeline boundaries, the
  alternatives that lost, and the rollout.

## Repo conventions

**Context stays where it belongs.** A service owns its own README, Makefile, tests,
`pyproject.toml` and Dockerfile under `services/<name>/`; a shared package owns the same
under `packages/<name>/`; their design documents live under `docs/<area>/<name>/`. Nothing
at the root accumulates service-specific detail.

**Make is two layers.** The root [`Makefile`](Makefile) is repo-level only — `doctor`,
`test`, `coverage`, `build`, `lock` — and delegates the rest to each service's own
Makefile, which owns `test`, `coverage`, `run` and `image`. Coverage is reported per
service, against that service's own code: one blended number would let a well-tested
service hide an untested one.

Working notes and plans live under `.claude/tasks/`; anything durable graduates to `docs/`.
Agent guidance is in [`AGENTS.md`](AGENTS.md); the per-language rule files under
`.claude/rules/` are still empty.

## Python dependencies

One uv workspace, one lock.

**Why a `pyproject.toml` in two places.** The root one *is* the workspace definition: it has
no `[project]` table, so it is virtual and not installable, and its only job is to list the
members. Each service needs its own because each has its own identity, its own dependencies
and its own `requires-python`. Neither can do the other's job.

**The lock is single and lives at the root**, and it should stay that way. A workspace
resolves as one set, so every service agrees on the version of anything they share. Adding a
per-service lock would reintroduce exactly the per-service resolution the workspace removed,
and give two files the authority to disagree.

Bounds live in each service's `pyproject.toml`; exact versions and hashes live in
[`uv.lock`](uv.lock), which is committed. `.python-version` at the root pins the interpreter
for the one workspace virtualenv, so uv never has to guess.

**Services are applications, not libraries.** Each sets `[tool.uv] package = false`, so uv
installs its dependencies but never builds or publishes it — they appear in the lock as
`virtual`. Shared code under `packages/` stays installable and appears as `editable`.

**Dependabot is the only update mechanism.** Its `uv` ecosystem reads the root manifest plus
the lock and can move transitive packages, which a flat requirements file never exposed.
