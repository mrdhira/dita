"""Liveness, readiness and startup, as protocol ops rather than URL paths."""

from __future__ import annotations

import os
import time
from typing import Any, Dict

from .manager import ModelManager


class Health:
    """What each probe is allowed to be false for.

    Kubernetes names, because the semantics are the familiar ones (`healthz` is deprecated
    and not offered). Health lives on the DIP socket, not on the metrics port, so a
    container healthcheck is an exec probe: `python -m <worker> --probe ready`.

      livez    process and accept loop are up; no dependency checks. False: restart me.
      readyz   can be given work. A load in flight is progress, not a wedge, so a cold
               load does not make this false -- `resident` says whether `infer` would
               succeed right now. False: stop routing to me.
      startupz boot finished: socket bound, registry parsed. False: still booting.
    """

    def __init__(self, manager: ModelManager) -> None:
        self._manager = manager
        self._started_at = time.monotonic()
        self._bound = False
        self._stopped = False

    def mark_bound(self) -> None:
        self._bound = True

    def mark_stopped(self) -> None:
        self._stopped = True

    def live(self) -> Dict[str, Any]:
        failures = []
        if self._stopped:
            failures.append("the server is shutting down")
        return self._verdict("livez", failures)

    def startup(self) -> Dict[str, Any]:
        failures = []
        if not self._bound:
            failures.append("the socket is not bound yet")
        if not self._manager.registry.models:
            failures.append("the registry parsed to no models")
        return self._verdict("startupz", failures)

    def ready(self) -> Dict[str, Any]:
        failures = []
        if not self._bound or self._stopped:
            failures.append("the socket is not serving")
        if not self._manager.registry.models:
            failures.append("the registry parsed to no models")

        models_dir = self._manager.models_dir
        if not os.access(models_dir, os.W_OK | os.X_OK):
            failures.append(f"the models directory {models_dir} is not writable")

        last_error = self._manager.last_error
        if last_error is not None and self._manager.resident() is None:
            failures.append(f"nothing is resident and {last_error}")

        verdict = self._verdict("readyz", failures)
        verdict["resident"] = self._manager.resident()
        verdict["loading"] = self._manager.loading
        return verdict

    def _verdict(self, probe: str, failures: list) -> Dict[str, Any]:
        return {
            "probe": probe,
            "status": "pass" if not failures else "fail",
            "uptime_s": round(time.monotonic() - self._started_at, 3),
            "reasons": failures,
        }
