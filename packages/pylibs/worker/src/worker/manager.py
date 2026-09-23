"""The one-model-at-a-time model manager.

Invariant: at most one engine is resident. ``load`` releases the previous engine before
constructing the next, so the memory budget is the largest single model, not the sum.

Downloading happens outside the exclusive lock and before anything is released, so a slow
or failing fetch neither blocks in-flight inference nor evicts a working model.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple, TypeVar

from . import fetcher
from .engines import Engine, EngineFactory
from .metrics import Metrics
from .registry import ModelSpec, Registry

LOG = logging.getLogger(__name__)

T = TypeVar("T")


class NoModelLoaded(Exception):
    """An inference was requested while nothing was resident."""


class ModelManager:
    def __init__(
        self,
        registry: Registry,
        models_dir: Path,
        build_engine: EngineFactory,
        metrics: Optional[Metrics] = None,
    ) -> None:
        self._registry = registry
        self._models_dir = models_dir
        self._metrics = metrics
        # The only thing the framework cannot know: which adapter an engine name means.
        self._build_engine = build_engine

        # Guards the resident engine and every swap of it. Held only for fast work.
        self._lock = threading.Lock()
        # Serialises downloads. Never held together with _lock, so a download blocks
        # nothing but another download.
        self._fetch_lock = threading.Lock()

        self._engine: Optional[Engine] = None
        # One attribute so a lock-free reader can never see a half-updated pair.
        self._resident: Optional[Tuple[ModelSpec, float]] = None
        self._loading: Optional[str] = None
        # Sticky until the next successful load or an unload, so readiness can tell a
        # worker with nothing loaded from one whose last load failed.
        self._last_error: Optional[str] = None

    @property
    def registry(self) -> Registry:
        return self._registry

    @property
    def models_dir(self) -> Path:
        return self._models_dir

    @property
    def last_error(self) -> Optional[str]:
        """Why the most recent load failed, or None if the last one worked."""
        return self._last_error

    def list(self) -> Dict[str, Any]:
        return {
            "models": self._registry.summaries(),
            "default_model": self._registry.default_model,
            "resident": self.resident(),
            "loading": self._loading,
        }

    def resident(self) -> Optional[Dict[str, Any]]:
        """The currently loaded model, or None. Lock-free and consistent by construction."""
        current = self._resident
        if current is None:
            return None
        spec, loaded_at = current
        return {
            "id": spec.id,
            "engine": spec.engine,
            "langs": list(spec.langs),
            "resident_seconds": round(time.monotonic() - loaded_at, 3),
        }

    @property
    def loading(self) -> Optional[str]:
        """The model id currently being fetched, if any. Informational."""
        return self._loading

    def load(self, model_id: str) -> Dict[str, Any]:
        spec = self._registry.get(model_id)
        started = time.monotonic()

        already = self._already_resident(spec)
        if already is not None:
            return already

        try:
            return self._load(spec, started)
        except Exception as exc:  # noqa: BLE001 - recorded for readiness, then re-raised
            self._last_error = f"load of {spec.id} failed: {type(exc).__name__}: {exc}"
            raise

    def _load(self, spec: ModelSpec, started: float) -> Dict[str, Any]:
        # Phase 1: get the bytes on disk. No exclusive lock and nothing released yet, so
        # the resident model keeps serving and survives a failed fetch untouched.
        with self._fetch_lock:
            self._loading = spec.id
            try:
                fetcher.ensure_model(self._models_dir, spec, self._metrics)
            finally:
                self._loading = None

        # Phase 2: swap. Release first, build second -- the invariant, not an oversight.
        # A build that fails here leaves nothing resident, which is the honest outcome.
        with self._lock:
            already = self._already_resident(spec)
            if already is not None:
                return already

            previous = self._resident[0].id if self._resident else None
            self._release()
            if previous is not None and self._metrics is not None:
                # The eviction is real the moment the engine is released, and the most
                # interesting eviction is the one whose build then fails.
                self._metrics.evicted(previous)
            engine = self._build_engine(
                spec.engine, fetcher.model_dir(self._models_dir, spec), spec.options
            )
            self._engine = engine
            self._resident = (spec, time.monotonic())

        self._last_error = None
        load_ms = round((time.monotonic() - started) * 1000, 1)
        if self._metrics is not None:
            self._metrics.loaded(spec.id, load_ms / 1000)
        LOG.info("loaded %s in %sms (unloaded %s)", spec.id, load_ms, previous or "nothing")
        return {
            "id": spec.id,
            "engine": spec.engine,
            "already_resident": False,
            "load_ms": load_ms,
            "unloaded": previous,
        }

    def unload(self) -> Dict[str, Any]:
        with self._lock:
            previous = self._resident[0].id if self._resident else None
            self._release()
            self._last_error = None
        if previous:
            LOG.info("unloaded %s", previous)
        return {"unloaded": previous}

    def infer(self, payload: bytes) -> Dict[str, Any]:
        if not payload:
            raise ValueError("infer needs the input in the message payload, which arrived empty")

        model_id, result, infer_ms = self.run(lambda engine: engine.infer(payload))
        response = result.as_dict()
        response["model"] = model_id
        response["infer_ms"] = infer_ms
        return response

    def run(self, work: Callable[[Engine], T]) -> Tuple[str, T, float]:
        """Call `work` with the resident engine, under the same lock and the same metrics as
        `infer`: (model id, what it returned, milliseconds). Never loads anything."""
        with self._lock:
            if self._engine is None or self._resident is None:
                raise NoModelLoaded(
                    "no model is loaded; send `load` with a model id from `list` before `infer`"
                )
            model_id = self._resident[0].id
            started = time.monotonic()
            value = work(self._engine)
            infer_ms = round((time.monotonic() - started) * 1000, 1)

        # Outside the lock: a metrics update must never extend the critical section.
        if self._metrics is not None:
            self._metrics.inferred(model_id, infer_ms / 1000)
        return model_id, value, infer_ms

    def _already_resident(self, spec: ModelSpec) -> Optional[Dict[str, Any]]:
        current = self._resident
        if current is None or current[0].id != spec.id:
            return None
        return {
            "id": spec.id,
            "engine": spec.engine,
            "already_resident": True,
            "load_ms": 0,
            "unloaded": None,
        }

    def _release(self) -> None:
        """Drop the resident engine. Caller must hold the lock."""
        if self._engine is not None:
            try:
                self._engine.close()
            except Exception:  # noqa: BLE001 - a failing close must not strand us mid-swap
                LOG.exception("closing the resident engine failed; dropping the reference anyway")
        self._engine = None
        self._resident = None
