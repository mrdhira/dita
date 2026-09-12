"""Download a model's files into $MODELS_DIR and prove they are the expected bytes.

Deliberately small: one HTTPS GET per file against the Hugging Face resolve endpoint,
pinned to an immutable commit sha. No hub client, no auth, no symlink cache.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Protocol, Tuple

from .registry import ModelFile, ModelSpec

LOG = logging.getLogger(__name__)


class MetricsSink(Protocol):
    """What the fetcher needs from metrics. Kept structural so this module imports nothing."""

    def fetched(self, byte_count: int) -> None: ...

DEFAULT_HF_ENDPOINT = "https://huggingface.co"
READ_CHUNK = 1024 * 1024
REQUEST_TIMEOUT = 120

# Hard stop for a file whose registry entry has no `bytes`, so a hostile or broken
# endpoint cannot stream until the disk fills.
MAX_UNDECLARED_BYTES = 2 * 1024 * 1024 * 1024

# Files whose digest this process has already checked, keyed by path. Cleared implicitly
# on restart, so a cold start still hashes everything once.
_verified: Dict[str, Tuple[int, int, str]] = {}
_verified_lock = threading.Lock()


class FetchError(Exception):
    """A file could not be downloaded."""


class ChecksumError(Exception):
    """A file is on disk but is not the file we pinned. Never silently accepted."""


def endpoint() -> str:
    """The download host. Overridable for mirrors and tests, but not to arbitrary schemes."""
    value = os.environ.get("HF_ENDPOINT", DEFAULT_HF_ENDPOINT).rstrip("/")
    scheme = urllib.parse.urlparse(value).scheme
    if scheme not in ("http", "https"):
        raise FetchError(f"HF_ENDPOINT must be an http or https URL, got {value!r}")
    if scheme == "http":
        LOG.warning("HF_ENDPOINT %s is plain http; digests still gate integrity", value)
    return value


def model_dir(models_dir: Path, spec: ModelSpec) -> Path:
    return models_dir / spec.id


def ensure_model(
    models_dir: Path, spec: ModelSpec, metrics: Optional["MetricsSink"] = None
) -> List[Path]:
    """Make every file of `spec` present and valid under $MODELS_DIR/<id>/.

    Files already present with the right digest are left alone. Returns the local paths
    in registry order.
    """
    if not spec.needs_download:
        return []

    unverified = [f.dest for f in spec.files if not f.verified]
    if unverified:
        raise ChecksumError(
            f"model '{spec.id}' has files with no pinned sha256 ({', '.join(unverified)}); "
            "verify them and record the digest in models.yaml before loading this model"
        )

    target_dir = model_dir(models_dir, spec)
    return [_ensure_file(target_dir, spec, spec_file, metrics) for spec_file in spec.files]


def _ensure_file(
    target_dir: Path, spec: ModelSpec, spec_file: ModelFile, metrics: Optional["MetricsSink"] = None
) -> Path:
    destination = target_dir / spec_file.dest

    if destination.exists():
        if _already_verified(destination, spec_file.sha256):
            LOG.debug("%s: %s already verified this run", spec.id, spec_file.dest)
            return destination

        stat = destination.stat()
        if spec_file.bytes is not None and stat.st_size != spec_file.bytes:
            LOG.warning(
                "%s: cached %s is %d bytes, expected %d -- re-downloading",
                spec.id,
                spec_file.dest,
                stat.st_size,
                spec_file.bytes,
            )
            destination.unlink()
        else:
            actual = _sha256(destination)
            if actual == spec_file.sha256:
                _remember(destination, spec_file.sha256)
                LOG.debug("%s: %s already present and valid", spec.id, spec_file.dest)
                return destination
            LOG.warning(
                "%s: cached %s has digest %s, expected %s -- re-downloading",
                spec.id,
                spec_file.dest,
                actual,
                spec_file.sha256,
            )
            destination.unlink()

    destination.parent.mkdir(parents=True, exist_ok=True)
    url = f"{endpoint()}/{spec_file.repo}/resolve/{spec_file.revision}/{spec_file.path}"
    partial = destination.with_suffix(destination.suffix + ".part")
    ceiling = spec_file.bytes if spec_file.bytes is not None else MAX_UNDECLARED_BYTES

    LOG.info("%s: downloading %s from %s", spec.id, spec_file.dest, url)
    try:
        written = _stream_to(url, partial, ceiling)
    except (urllib.error.URLError, OSError) as exc:
        partial.unlink(missing_ok=True)
        raise FetchError(f"could not download {url}: {exc}") from exc
    except FetchError:
        partial.unlink(missing_ok=True)
        raise

    if spec_file.bytes is not None and written != spec_file.bytes:
        partial.unlink(missing_ok=True)
        raise ChecksumError(f"{url} is {written} bytes, models.yaml pins {spec_file.bytes}")

    actual = _sha256(partial)
    if actual != spec_file.sha256:
        partial.unlink(missing_ok=True)
        raise ChecksumError(
            f"{url} has sha256 {actual}, models.yaml pins {spec_file.sha256}; refusing to use it"
        )

    partial.replace(destination)
    _remember(destination, spec_file.sha256)
    if metrics is not None:
        metrics.fetched(written)
    LOG.info("%s: %s verified (%s)", spec.id, spec_file.dest, spec_file.sha256[:12])
    return destination


def _stream_to(url: str, partial: Path, ceiling: int) -> int:
    """Copy `url` into `partial`, refusing to write more than `ceiling` bytes."""
    written = 0
    with urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT) as response, partial.open("wb") as out:
        while True:
            block = response.read(READ_CHUNK)
            if not block:
                return written
            written += len(block)
            if written > ceiling:
                raise FetchError(f"{url} sent more than the {ceiling} bytes models.yaml pins")
            out.write(block)


def _already_verified(path: Path, sha256: str) -> bool:
    """True if this process already hashed exactly these bytes at this path.

    Guards against re-hashing hundreds of megabytes on every load. The identity is
    (size, mtime_ns, digest), so any rewrite of the file forces a fresh hash.
    """
    try:
        stat = path.stat()
    except OSError:
        return False
    with _verified_lock:
        remembered = _verified.get(str(path))
    return remembered == (stat.st_size, stat.st_mtime_ns, sha256)


def _remember(path: Path, sha256: str) -> None:
    stat = path.stat()
    with _verified_lock:
        _verified[str(path)] = (stat.st_size, stat.st_mtime_ns, sha256)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(READ_CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()
