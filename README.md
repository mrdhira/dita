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

Deployment lives in [`deployment/docker-compose.yml`](deployment/docker-compose.yml).

## Shared packages

`packages/` is organised **language-first**, because the tooling is:

```
packages/
  golibs/
    dip/            the protocol: framing, both roles, generated types
  pylibs/
    dip/            the same protocol, the same generated types, in Python
    dita-worker/    everything an inference worker does except the inference
```

A worker service is now composition: `dita-worker` brings the socket server, the one-model
manager, the registry reader, the digest-checking fetcher, the health probes, the CLI and
the metrics, and the service brings an engine and a `models.yaml`. That claim is held up by
a test in the package that builds a complete working worker from a fake engine and drives
it over a real socket, importing nothing from any service.

Each toolchain globs only its own subtree — `[tool.uv.workspace] members` covers
`packages/pylibs/*`, `go.work` covers modules under `packages/golibs/` — so neither tool
tries to own the other's tree, and `packages/` itself is not a service.

`go.work` takes no glob, so a new Go module is added with `make go-work-sync`
(`go work use -r`), which expands it into the explicit list.

**The one exception to language-first is the protocol.** DIP has a single
language-neutral definition with two implementations, so it cannot live under either
language's directory without one looking authoritative:

```
specs/dip/               the IDL (JSON Schema 2020-12) and the conformance corpus
docs/protocol/           the prose specification
packages/golibs/dip/     the Go implementation, types generated from the IDL
packages/pylibs/dip/     the Python implementation, types generated from the IDL
```

See [the DIP specification](docs/protocol/%5B1%5Ddip-specification.md) for what the protocol
is and why it is a new one rather than gRPC, Cap'n Proto, NDJSON or HTTP.

**Generated code has zero third-party dependencies**, which is an acceptance criterion, not
a preference. `make dip-verify` proves it both ways: `go list -deps` must report no
module-path package, and an AST scan must find no import outside the standard library.
`make dip-generate` regenerates both languages from the IDL; the output is committed. Both
generators are dev-only tools and neither reaches a runtime image.

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
`python3` happens to be first on your PATH. If it is missing, `uv python install 3.14`.

```bash
make test        # every unit's tests, both languages
make coverage    # every unit's coverage, reported per unit
make build       # every service image
docker compose -f deployment/docker-compose.yml up --build
```

## Metrics

Each worker serves Prometheus text on its own small HTTP port, separate from the workload
protocol: DIP is the hot path and stays a unix socket, while a scrape is a different
concern with different traffic. The port defaults to loopback and is never published to the
host; compose binds it on the internal network so a scraper can reach it.

The metrics stack is **opt-in**, because the default `up` should stay small:

```bash
docker compose -f deployment/docker-compose.yml \
               -f deployment/compose.observability.yml \
               --profile observability up -d
```

That runs [VictoriaMetrics](https://docs.victoriametrics.com/), which is the pick because
**vmui replaces Grafana** — query, explore and graph at
[localhost:8428/vmui](http://localhost:8428/vmui) with nothing else to run or configure.
Without the profile flag nothing observability-related starts.

Scrape targets live in
[`deployment/observability/scrape.yml`](deployment/observability/scrape.yml). The Go
orchestrator will expose its own `/metrics` later; its target is already written there,
commented out, and the stack will scrape it with no other change.

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

- [`docs/protocol/[1]dip-specification.md`](docs/protocol/%5B1%5Ddip-specification.md)
  — DIP: what it is, why it is a new protocol rather than gRPC or HTTP, the framing, the ops
  and the versioning rules.
- [`docs/protocol/[2]open-questions.md`](docs/protocol/%5B2%5Dopen-questions.md)
  — two decisions waiting on Dhira: how far the response shape should generalise beyond OCR,
  and what a minimal CI gate would need.
- [`docs/inferences/ocr/[1]technical-requirement.md`](docs/inferences/ocr/%5B1%5Dtechnical-requirement.md)
  — the OCR worker: context, the per-engine pipeline boundaries, the alternatives that lost,
  the metrics contract, and the rollout.

## Repo conventions

**Context stays where it belongs.** A service owns its own README, Makefile, tests,
`pyproject.toml` and Dockerfile under `services/<name>/`; a shared package owns the same
under `packages/<name>/`; their design documents live under `docs/<area>/<name>/`. Nothing
at the root accumulates service-specific detail.

**Make is two layers.** The root [`Makefile`](Makefile) is repo-level only — `doctor`,
`test`, `coverage`, `build`, `lock`, and the DIP codegen targets — and delegates the rest to
each unit's own Makefile, which owns `test`, `coverage` and whatever else that unit needs.

A **unit** is anything with its own tests and its own coverage number: a service, a shared
package, or an example. **Examples count as units.** They are code someone will copy, so
they get the same treatment and their own number; folding them into the service they
demonstrate would hide whether they are exercised at all.

Coverage is reported per unit, against that unit's own code, in both languages. One blended
number would let a well-tested unit hide an untested one.

Working notes and plans live under `.claude/tasks/`; anything durable graduates to `docs/`.
Agent guidance is in [`AGENTS.md`](AGENTS.md), with the per-language rules in
[`.claude/rules/`](.claude/rules/) — Python, Go and QA — and the subagents that read them in
[`.claude/agents/`](.claude/agents/).

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
[`uv.lock`](uv.lock), which is committed.

**Python is pinned to an exact patch, 3.14.6**, in three places that must agree:
[`.python-version`](.python-version) for the workspace virtualenv, `requires-python` in each
service, and the base image tag `python:3.14.6-slim-trixie`. `make doctor` asserts the exact
version rather than the minor, because a rolling tag changes what you shipped without
changing anything you wrote.

**Services are applications, not libraries.** Each sets `[tool.uv] package = false`, so uv
installs its dependencies but never builds or publishes it — they appear in the lock as
`virtual`. Shared code under `packages/` stays installable and appears as `editable`.

**Dependabot is the only update mechanism**, across three ecosystems: `uv` for the Python
workspace, `docker` for the pinned base image, and `gomod` for the Go modules. The `uv`
ecosystem reads the root manifest plus the lock and can move transitive packages, which a
flat requirements file never exposed.

## Go modules

Every `go.mod` carries `go 1.27.1` and [`go.work`](go.work) carries both `go 1.27.1` and
`toolchain go1.27.1`, matching [`.tool-versions`](.tool-versions). `make doctor` fails if any
of them drift. The compiler on your `PATH` is not what builds this repo; these directives are.

A `go` directive at the full patch **is** the pin, and a `toolchain` directive is only
meaningful in a `go.mod` when it names a version *newer* than the `go` line. Setting both to
1.27.1 makes the module untidy: `go build` refuses with "updates to go.mod needed" and
`go mod tidy` deletes the line. `go.work` is the one file that accepts both.

[`go.work.sum`](go.work.sum) and the `go.sum` of every stdlib-only module are committed
while empty, so the layout is complete from day one. **An empty lockfile is not a lock** —
they stay empty until that module takes a third-party dependency, and the `gomod` Dependabot
entry is what notices the day one arrives. `services/dita-orchestrator` already has real
dependencies, so its `go.sum` has real content.
