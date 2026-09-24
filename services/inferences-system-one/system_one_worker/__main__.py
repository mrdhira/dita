"""Entry point: python -m system_one_worker. Everything this file does not do is `worker`'s;
what is System One is the engine factory, the manifest beside it and the /decide routes."""

from __future__ import annotations

import sys
from pathlib import Path

from worker import Worker, cli

from . import __version__, decide
from .engines import ENGINE_NAMES, build_engine

REGISTRY_PATH = Path(__file__).resolve().parent.parent / "models.yaml"

WORKER = Worker(
    name="inferences-system-one",
    version=__version__,
    engines=ENGINE_NAMES,
    build_engine=build_engine,
    registry_path=REGISTRY_PATH,
    prog="system_one_worker",
    log_level_env="SYSTEM_ONE_LOG_LEVEL",
    routes=decide.routes(),
)


def main(argv: list[str] | None = None) -> int:
    return cli.main(WORKER, argv)


if __name__ == "__main__":
    sys.exit(main())
