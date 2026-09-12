"""Prometheus text metrics, served on a small HTTP port of their own.

A scrape is not the workload. DIP stays a unix socket because it is the hot path and
carries megabytes; metrics are a few kilobytes of text on a timer, and every scraper in
existence already speaks HTTP. Serving them here costs no runtime dependency: `http.server`
and a dict.

Two rules this module exists to keep:

**A scrape never touches the worker's lock.** Counters live under their own lock, held for
a dict update and nothing else, and the gauges are read from `ModelManager`'s lock-free
`resident`/`loading` view. An inference in progress cannot be delayed by a scrape, and a
scrape cannot be delayed by an inference.

**The numbers are real.** Every counter is incremented on the path it names; nothing here
is a placeholder. A counter that never moves is worse than none, because it reads as
"nothing is happening" rather than "nothing is measured".
"""

from __future__ import annotations

import http.server
import logging
import os
import resource
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

LOG = logging.getLogger(__name__)

# Seconds. Load is a download plus a session init; inference is a page. The buckets are
# chosen to straddle what this worker actually does rather than to look tidy.
LOAD_BUCKETS = (0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 300.0)
INFER_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)

DEFAULT_ADDR = "127.0.0.1:9109"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels(pairs: Iterable[Tuple[str, str]]) -> str:
    rendered = ",".join(f'{name}="{_escape(value)}"' for name, value in pairs)
    return f"{{{rendered}}}" if rendered else ""


class Histogram:
    """Cumulative buckets, a sum and a count, which is what Prometheus wants."""

    def __init__(self, buckets: Tuple[float, ...]) -> None:
        self.buckets = buckets
        self.counts: Dict[str, List[int]] = {}
        self.sums: Dict[str, float] = {}
        self.totals: Dict[str, int] = {}

    def observe(self, key: str, seconds: float) -> None:
        counts = self.counts.setdefault(key, [0] * len(self.buckets))
        for index, edge in enumerate(self.buckets):
            if seconds <= edge:
                counts[index] += 1
        self.sums[key] = self.sums.get(key, 0.0) + seconds
        self.totals[key] = self.totals.get(key, 0) + 1


class Metrics:
    """Everything the worker counts. Safe to call from any thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started = time.monotonic()

        self.ops: Dict[Tuple[str, str], int] = {}
        self.errors: Dict[str, int] = {}
        self.loads: Dict[str, int] = {}
        self.evictions = 0
        self.fetched_bytes = 0
        self.fetched_files = 0
        self.connections_accepted = 0
        self.connections_refused = 0
        self.load_seconds = Histogram(LOAD_BUCKETS)
        self.infer_seconds = Histogram(INFER_BUCKETS)

    def op(self, op: str, outcome: str) -> None:
        with self._lock:
            self.ops[(op, outcome)] = self.ops.get((op, outcome), 0) + 1

    def error(self, code: str) -> None:
        with self._lock:
            self.errors[code] = self.errors.get(code, 0) + 1

    def loaded(self, model_id: str, seconds: float, evicted: Optional[str]) -> None:
        with self._lock:
            self.loads[model_id] = self.loads.get(model_id, 0) + 1
            self.load_seconds.observe(model_id, seconds)
            if evicted:
                self.evictions += 1

    def inferred(self, model_id: str, seconds: float) -> None:
        with self._lock:
            self.infer_seconds.observe(model_id, seconds)

    def fetched(self, byte_count: int) -> None:
        with self._lock:
            self.fetched_bytes += byte_count
            self.fetched_files += 1

    def connection(self, accepted: bool) -> None:
        with self._lock:
            if accepted:
                self.connections_accepted += 1
            else:
                self.connections_refused += 1

    def render(self, worker_name: str, manager: Any) -> str:
        """The Prometheus text body. Reads the manager lock-free; never blocks inference."""
        # Snapshot under our own lock so a concurrent update cannot tear the output.
        with self._lock:
            ops = dict(self.ops)
            errors = dict(self.errors)
            loads = dict(self.loads)
            evictions = self.evictions
            fetched_bytes, fetched_files = self.fetched_bytes, self.fetched_files
            accepted, refused = self.connections_accepted, self.connections_refused
            load_hist = (dict(self.load_seconds.counts), dict(self.load_seconds.sums),
                         dict(self.load_seconds.totals))
            infer_hist = (dict(self.infer_seconds.counts), dict(self.infer_seconds.sums),
                          dict(self.infer_seconds.totals))

        resident = manager.resident() if manager is not None else None
        loading = manager.loading if manager is not None else None
        usage = resource.getrusage(resource.RUSAGE_SELF)

        out: List[str] = []

        def emit(name: str, kind: str, help_text: str, samples: Iterable[Tuple[str, Any]]) -> None:
            rows = list(samples)
            if not rows:
                return
            out.append(f"# HELP {name} {help_text}")
            out.append(f"# TYPE {name} {kind}")
            out.extend(f"{name}{label}{' ' if label else ' '}{value}" for label, value in rows)

        worker = ("worker", worker_name)

        emit("dita_worker_ops_total", "counter",
             "DIP requests handled, by op and outcome.",
             [(_labels([worker, ("op", op), ("outcome", outcome)]), count)
              for (op, outcome), count in sorted(ops.items())])

        emit("dita_worker_errors_total", "counter",
             "Failed requests by DIP error code.",
             [(_labels([worker, ("code", code)]), count) for code, count in sorted(errors.items())])

        emit("dita_worker_model_loads_total", "counter",
             "Successful model loads, by model.",
             [(_labels([worker, ("model", model)]), count) for model, count in sorted(loads.items())])

        emit("dita_worker_model_evictions_total", "counter",
             "Loads that displaced a resident model. One model is resident at a time.",
             [(_labels([worker]), evictions)])

        emit("dita_worker_fetched_bytes_total", "counter",
             "Model bytes downloaded and digest-verified.",
             [(_labels([worker]), fetched_bytes)])
        emit("dita_worker_fetched_files_total", "counter",
             "Model files downloaded and digest-verified.",
             [(_labels([worker]), fetched_files)])

        emit("dita_worker_connections_total", "counter",
             "Socket connections, by whether they were accepted or refused at the cap.",
             [(_labels([worker, ("outcome", "accepted")]), accepted),
              (_labels([worker, ("outcome", "refused")]), refused)])

        _emit_histogram(out, "dita_worker_load_duration_seconds",
                        "Model load duration, including download and session init.",
                        LOAD_BUCKETS, load_hist, worker)
        _emit_histogram(out, "dita_worker_infer_duration_seconds",
                        "Inference duration, excluding load.",
                        INFER_BUCKETS, infer_hist, worker)

        emit("dita_worker_model_resident", "gauge",
             "1 when a model is resident and able to answer infer right now.",
             [(_labels([worker, ("model", resident["id"])] if resident else [worker]),
               1 if resident else 0)])
        emit("dita_worker_model_resident_seconds", "gauge",
             "How long the resident model has been loaded.",
             [(_labels([worker]), resident["resident_seconds"] if resident else 0)])
        emit("dita_worker_model_loading", "gauge",
             "1 while a model is being fetched. A load in flight is progress, not a wedge.",
             [(_labels([worker, ("model", loading)] if loading else [worker]),
               1 if loading else 0)])

        emit("dita_worker_uptime_seconds", "gauge", "Process uptime.",
             [(_labels([worker]), round(time.monotonic() - self._started, 3))])
        emit("process_resident_memory_bytes", "gauge", "Resident set size.",
             [(_labels([worker]), usage.ru_maxrss * 1024)])
        emit("process_cpu_seconds_total", "counter", "User plus system CPU time.",
             [(_labels([worker]), round(usage.ru_utime + usage.ru_stime, 3))])

        return "\n".join(out) + "\n"


def _emit_histogram(out: List[str], name: str, help_text: str, buckets: Tuple[float, ...],
                    snapshot: Tuple[Dict[str, List[int]], Dict[str, float], Dict[str, int]],
                    worker: Tuple[str, str]) -> None:
    counts, sums, totals = snapshot
    if not totals:
        return
    out.append(f"# HELP {name} {help_text}")
    out.append(f"# TYPE {name} histogram")
    for model in sorted(totals):
        for edge, count in zip(buckets, counts[model]):
            out.append(f'{name}_bucket{_labels([worker, ("model", model), ("le", str(edge))])} {count}')
        out.append(f'{name}_bucket{_labels([worker, ("model", model), ("le", "+Inf")])} {totals[model]}')
        out.append(f'{name}_sum{_labels([worker, ("model", model)])} {round(sums[model], 6)}')
        out.append(f'{name}_count{_labels([worker, ("model", model)])} {totals[model]}')


def parse_addr(value: str) -> Tuple[str, int]:
    host, _, port = value.rpartition(":")
    if not host or not port.isdigit():
        raise ValueError(f"metrics address must be host:port, got {value!r}")
    return host, int(port)


def serve(metrics: Metrics, worker_name: str, manager: Any, address: Tuple[str, int]):
    """Start the metrics server on its own daemon thread. Returns the server."""

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "dita-worker-metrics"

        def do_GET(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
            if self.path.split("?")[0] not in ("/metrics", "/"):
                self.send_error(404, "only /metrics")
                return
            body = metrics.render(worker_name, manager).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: object) -> None:
            LOG.debug("metrics %s - %s", self.address_string(), fmt % args)

    server = http.server.ThreadingHTTPServer(address, Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True, name="dita-metrics").start()
    LOG.info("metrics on http://%s:%d/metrics", *address)
    return server


def address_from_env(env: str = "METRICS_ADDR") -> Optional[Tuple[str, int]]:
    """Loopback by default, so metrics are never exposed by accident."""
    value = os.environ.get(env, DEFAULT_ADDR).strip()
    if not value or value.lower() in ("off", "none", "disabled"):
        return None
    return parse_addr(value)
