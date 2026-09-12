# dita-worker

> Everything an inference worker does except the inference: one resident model, weights
> pinned by digest, and a DIP socket. A service brings an engine and a manifest.

**Status: v0.** Extracted from `services/inferences-ocr`, which is where this code came
from and is now its first caller. `services/inferences-stt` should need nothing from here
but the two things below.

- **One model resident, ever.** `load` releases the previous engine *before* building the
  next one, so the memory budget is the largest model rather than the sum
- **Weights are never trusted.** `models.yaml` pins every file to an immutable revision and
  a sha256; a file with no digest makes its model unloadable, not trusted
- **No implicit loading.** `infer` with nothing resident answers `no_model_loaded`; it does
  not pick a model for the caller
- **The wire is [`dip`](../dip)**, not a copy of it. Nothing here reimplements framing

## What is in it

| module | what it owns |
| --- | --- |
| `worker.py` | `Worker`: the seam — what a service tells the framework about itself |
| `registry.py` | the models.yaml reader, and every malformed manifest it refuses |
| `fetcher.py` | one HTTPS GET per file, the digest check, and the refusal to keep bad bytes |
| `manager.py` | `ModelManager`: the one-model-resident invariant and the fetch-then-swap order |
| `server.py` | `dispatch` and the AF_UNIX accept loop, including the connection cap |
| `health.py` | `livez` / `readyz` / `startupz`, as protocol ops rather than URL paths |
| `engines.py` | the `Engine` ABC, `Line`/`Result`, and the factory type a service supplies |
| `cli.py` | the flags, the env vars, `--probe`, and the serve loop |
| `metrics.py` | the Prometheus counters and the small HTTP port that serves them |

What is **not** here is any knowledge of a model format, a media type or an engine. The
package never names an engine; it is handed a factory and calls it.

## Writing a worker

Two files. First the engine — bytes in, a `Result` out:

```python
from dita_worker import Engine, Line, Result, UnknownEngine

class WhisperEngine(Engine):
    def __init__(self, model_dir, options):
        super().__init__(model_dir, options)
        self._session = load_the_expensive_thing(model_dir)   # `load` means "ready"

    def infer(self, payload: bytes) -> Result:
        text = self._session.transcribe(payload)
        return Result(text=text, lines=[Line(text=text, confidence=None, box=None)])

    def close(self) -> None:      # called on every swap, and twice is allowed
        self._session = None

def build_engine(name, model_dir, options) -> Engine:
    if name == "whisper":
        return WhisperEngine(model_dir, options)
    raise UnknownEngine(f"engine '{name}' is not implemented")
```

Then `__main__.py`, which is the whole of the service's plumbing:

```python
from dita_worker import Worker, cli

WORKER = Worker(
    name="inferences-stt",                 # what `handshake` reports, and /run/dita/<name>.sock
    version=__version__,
    engines=("whisper",),                  # advertised in the handshake
    build_engine=build_engine,
    registry_path=Path(__file__).resolve().parent.parent / "models.yaml",
    prog="stt_worker",
    log_level_env="STT_LOG_LEVEL",
)

sys.exit(cli.main(WORKER))
```

A `models.yaml` beside it lists the models, and that is the service. `tests/test_worker_skeleton.py`
is exactly this, in one file, driven over a real socket — read it before writing a new
worker, and copy it.

### The flags every worker gets

`--socket` (`$SOCKET_PATH`), `--models-dir` (`$MODELS_DIR`), `--registry`
(`$MODELS_REGISTRY`), `--preload <id>` (`$PRELOAD_MODEL`), `--log-level`, `--version`, and
`--probe live|ready|startup` — a one-shot health check that exits 0 or 1, which is what a
container `HEALTHCHECK` execs for a service with no HTTP surface.

Exit codes: `2` the registry would not parse, `3` the socket directory cannot be used.

## Dependencies

`dip`, as a workspace source, and PyYAML. PyYAML is the one third-party import in the
package and it is confined to `registry.py`: a model manifest is YAML. `dip` itself stays
stdlib-only, which is what `make dip-verify` enforces.

## Tests

```bash
make test
make coverage        # the same suite under coverage, with a floor
```

Stdlib only, offline, no weights and no engines: the fixtures are a three-model manifest
with `system` sources and a fake engine that records its own lifecycle
(`tests/support.py`). Tables (`subTest`, one row per case) for the manifest refusals, the
fetcher refusals, the malformed requests and the exact line `--probe` prints. Scenarios
(plain methods, deliberately not tables) for the stateful things: concurrent loads, a
failed fetch, a cold load in flight, a stalled peer and the connection cap.

`tests/test_worker_skeleton.py` is the acceptance test for the split. If a change here
makes a new worker need anything the skeleton does not already have, that is the signal
the boundary moved.

## Building a new worker

`inferences-stt` is an engine and a manifest. Nothing else:

```python
# stt_worker/engines.py -- the only part that knows what inference means
from dita_worker import Engine, Line, Result

class WhisperEngine(Engine):
    def infer(self, payload: bytes) -> Result:
        text = transcribe(payload)
        return Result(text=text, lines=[Line(text=text, confidence=None, box=None)])

def build_engine(name, model_dir, options):
    if name == "whisper":
        return WhisperEngine(model_dir, options)
    raise UnknownEngine(f"engine '{name}' is not implemented")
```

```python
# stt_worker/__main__.py -- the whole entrypoint
WORKER = Worker(
    name="inferences-stt",
    version=__version__,
    engines=("whisper",),
    build_engine=build_engine,
    registry_path=Path(__file__).parent.parent / "models.yaml",
    prog="stt_worker",
)
sys.exit(cli.main(WORKER))
```

Plus a `models.yaml` pinning the weights. That is the whole service: the socket, the
framing, the one-model invariant, the digest checks, the health probes, the CLI, the
`--probe` exec healthcheck and the metrics all arrive with the package.

This is not a claim, it is a test.
[`tests/test_worker_skeleton.py`](tests/test_worker_skeleton.py) builds exactly that — a
six-line engine, a temporary two-model manifest, a `Worker` — and drives it over a real
socket through handshake, list, load, infer and unload. It imports nothing from any service.
If the seam ever leaks, that test stops compiling.

## Metrics

Every worker built on this package gets `/metrics` on a small HTTP port of its own,
Prometheus text format, no runtime dependency beyond `http.server`.

```
METRICS_ADDR=127.0.0.1:9109   # the default; `off` disables it entirely
```

Loopback by default, so a worker never exposes it by accident. `deployment/` binds it on
the compose network instead, and publishes nothing.

| metric | type | labels |
| --- | --- | --- |
| `dita_worker_ops_total` | counter | `op`, `outcome` |
| `dita_worker_errors_total` | counter | `code` |
| `dita_worker_model_loads_total` | counter | `model` |
| `dita_worker_model_evictions_total` | counter | |
| `dita_worker_fetched_bytes_total`, `..._files_total` | counter | |
| `dita_worker_connections_total` | counter | `outcome` |
| `dita_worker_load_duration_seconds` | histogram | `model` |
| `dita_worker_infer_duration_seconds` | histogram | `model` |
| `dita_worker_model_resident`, `..._resident_seconds`, `..._loading` | gauge | `model` |
| `process_resident_memory_bytes`, `process_cpu_seconds_total`, `dita_worker_uptime_seconds` | | |

Two properties the tests hold to, because both are easy to lose:

**A scrape never waits on the worker's lock.** Counters have their own lock, held for a
dict update; the gauges come from `ModelManager`'s lock-free view. `test_metrics.py` holds
the manager's lock open with a slow engine and asserts a scrape still returns.

**The numbers are real.** Each counter is incremented on the path it names, and each is
asserted to move around an actual call rather than merely to appear in the output.
