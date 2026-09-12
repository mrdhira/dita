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
import socket
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

LOG = logging.getLogger(__name__)

# Seconds. Load is a download plus a session init; inference is a page. The buckets are
# chosen to straddle what this worker actually does rather than to look tidy.
LOAD_BUCKETS = (0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 300.0)
INFER_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)

DEFAULT_ADDR = "127.0.0.1:9109"

# Seconds a scrape connection may sit idle before it is closed. HTTP/1.1 keep-alive means
# a client that connects and says nothing otherwise pins a thread forever.
REQUEST_TIMEOUT = 10.0
# Live scrape connections. The DIP socket has a cap for the same reason; this port is
# smaller but just as reachable, and this process has one memory budget.
MAX_SCRAPES = 8

# Linux reports the resident set in pages; statm's second field is the current one.
PAGE_SIZE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
STATM = "/proc/self/statm"


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

    def snapshot(self) -> Tuple[Dict[str, List[int]], Dict[str, float], Dict[str, int]]:
        """A copy to render outside the lock. The bucket lists are copied too: `observe`
        mutates them in place, and sharing one would let a scrape emit a bucket above the
        `+Inf` bucket and above `_count`, which silently corrupts histogram_quantile."""
        return (
            {key: list(counts) for key, counts in self.counts.items()},
            dict(self.sums),
            dict(self.totals),
        )


class Metrics:
    """Everything the worker counts. Safe to call from any thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started = time.monotonic()

        self.ops: Dict[Tuple[str, str], int] = {}
        self.errors: Dict[str, int] = {}
        self.loads: Dict[str, int] = {}
        self.evictions = 0
        # The last model this worker had resident. The gauges keep naming it after it is
        # gone, so the series goes to zero instead of vanishing and an alert can see it.
        self.last_model: Optional[str] = None
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

    def loaded(self, model_id: str, seconds: float) -> None:
        with self._lock:
            self.loads[model_id] = self.loads.get(model_id, 0) + 1
            self.load_seconds.observe(model_id, seconds)
            self.last_model = model_id

    def evicted(self, model_id: str) -> None:
        """A resident model was released to make room for another. Counted where the
        release happens, not where the load succeeds: a build that then fails has still
        evicted, and that is the eviction worth seeing."""
        with self._lock:
            self.evictions += 1
            self.last_model = model_id

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
            last_model = self.last_model
            load_hist = self.load_seconds.snapshot()
            infer_hist = self.infer_seconds.snapshot()

        resident = manager.resident() if manager is not None else None
        loading = manager.loading if manager is not None else None
        # Empty is how Prometheus spells "no label": a worker that has never loaded
        # anything reports the same series it always did.
        model = (resident["id"] if resident else None) or loading or last_model or ""
        usage = resource.getrusage(resource.RUSAGE_SELF)

        out: List[str] = []

        def emit(name: str, kind: str, help_text: str, samples: Iterable[Tuple[str, Any]]) -> None:
            # The family is declared even with no samples in it. A counter that only
            # appears after its first event reads as a metric nobody exports, and both
            # rate() and absent() misbehave against a worker that has just started.
            out.append(f"# HELP {name} {help_text}")
            out.append(f"# TYPE {name} {kind}")
            out.extend(f"{name}{label} {value}" for label, value in samples)

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

        # All three carry `model`, always, so an unload takes the series to zero instead
        # of deleting it. A label that changed with the value could not be alerted on.
        named = _labels([worker, ("model", model)])
        emit("dita_worker_model_resident", "gauge",
             "1 when a model is resident and able to answer infer right now.",
             [(named, 1 if resident else 0)])
        emit("dita_worker_model_resident_seconds", "gauge",
             "How long the resident model has been loaded.",
             [(named, resident["resident_seconds"] if resident else 0)])
        emit("dita_worker_model_loading", "gauge",
             "1 while a model is being fetched. A load in flight is progress, not a wedge.",
             [(named, 1 if loading else 0)])

        emit("dita_worker_uptime_seconds", "gauge", "Process uptime.",
             [(_labels([worker]), round(time.monotonic() - self._started, 3))])
        resident_bytes = resident_memory_bytes()
        if resident_bytes is not None:
            emit("process_resident_memory_bytes", "gauge", "Resident set size, right now.",
                 [(_labels([worker]), resident_bytes)])
        # Named for what it is. The question this worker's memory gauge has to answer is
        # whether an eviction freed anything, and a high-water mark never falls.
        emit("process_resident_memory_peak_bytes", "gauge",
             "Peak resident set size since the process started.",
             [(_labels([worker]), usage.ru_maxrss * 1024)])
        emit("process_cpu_seconds_total", "counter", "User plus system CPU time.",
             [(_labels([worker]), round(usage.ru_utime + usage.ru_stime, 3))])

        return "\n".join(out) + "\n"


def _emit_histogram(out: List[str], name: str, help_text: str, buckets: Tuple[float, ...],
                    snapshot: Tuple[Dict[str, List[int]], Dict[str, float], Dict[str, int]],
                    worker: Tuple[str, str]) -> None:
    counts, sums, totals = snapshot
    out.append(f"# HELP {name} {help_text}")
    out.append(f"# TYPE {name} histogram")
    for model in sorted(totals):
        for edge, count in zip(buckets, counts[model]):
            out.append(f'{name}_bucket{_labels([worker, ("model", model), ("le", str(edge))])} {count}')
        out.append(f'{name}_bucket{_labels([worker, ("model", model), ("le", "+Inf")])} {totals[model]}')
        out.append(f'{name}_sum{_labels([worker, ("model", model)])} {round(sums[model], 6)}')
        out.append(f'{name}_count{_labels([worker, ("model", model)])} {totals[model]}')


def resident_memory_bytes(statm: str = STATM) -> Optional[int]:
    """Current RSS, which is the number an eviction is supposed to move. `ru_maxrss` is a
    high-water mark and never falls, so it cannot answer that. None where there is no
    /proc to read: a gauge that is not exported beats one that is wrong."""
    try:
        with open(statm, "r", encoding="ascii") as handle:
            fields = handle.read().split()
        return int(fields[1]) * PAGE_SIZE
    except (OSError, IndexError, ValueError):
        return None


def parse_addr(value: str) -> Tuple[str, int]:
    """`host:port`, or `[::1]:port` for IPv6 -- the brackets are the only way to say which
    colon separates the port."""
    host, _, port = value.rpartition(":")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if not host or not port.isdigit():
        raise ValueError(f"metrics address must be host:port, got {value!r}")
    return host, int(port)


def address_family(host: str) -> int:
    """A literal IPv6 host needs an IPv6 socket; a name or an IPv4 literal does not."""
    return socket.AF_INET6 if ":" in host else socket.AF_INET


class _ScrapeServer(http.server.ThreadingHTTPServer):
    """ThreadingHTTPServer with a ceiling on live connections.

    Keep-alive means a connection outlives its request, and a thread outlives the
    connection, so without a cap a handful of clients that connect and never speak are
    enough to exhaust the process. The DIP socket refuses at a cap for the same reason.
    """

    daemon_threads = True

    def __init__(self, address: Tuple[str, int], handler: Any, max_scrapes: int) -> None:
        self.address_family = address_family(address[0])
        self._cap = max_scrapes
        self._slots = threading.BoundedSemaphore(max_scrapes)
        super().__init__(address, handler)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):
            LOG.warning("refusing a scrape: %d already open", self._cap)
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def serve(
    metrics: Metrics,
    worker_name: str,
    manager: Any,
    address: Tuple[str, int],
    request_timeout: float = REQUEST_TIMEOUT,
    max_scrapes: int = MAX_SCRAPES,
):
    """Start the metrics server on its own daemon thread. Returns the server."""

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "dita-worker-metrics"
        # Honoured by StreamRequestHandler.setup(): a client that connects and sends
        # nothing is closed rather than holding a thread until it feels like talking.
        timeout = request_timeout

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

        def version_string(self) -> str:
            # The default appends the exact interpreter version, which tells an
            # unauthenticated client which CPython to go and look up.
            return self.server_version

        def log_message(self, fmt: str, *args: object) -> None:
            LOG.debug("metrics %s - %s", self.address_string(), fmt % args)

    server = _ScrapeServer(address, Handler, max_scrapes)
    threading.Thread(target=server.serve_forever, daemon=True, name="dita-metrics").start()
    LOG.info("metrics on http://%s:%d/metrics", *server.server_address[:2])
    return server


def address_from_env(env: str = "METRICS_ADDR") -> Optional[Tuple[str, int]]:
    """Loopback by default, so metrics are never exposed by accident."""
    value = os.environ.get(env, DEFAULT_ADDR).strip()
    if not value or value.lower() in ("off", "none", "disabled"):
        return None
    return parse_addr(value)
