"""Entry point: python -m embedding_worker. Everything this file does not do is `worker`'s;
what is embedding is the engine factory, the manifest beside it and the TEI routes."""

from __future__ import annotations

import sys
from pathlib import Path

from worker import Worker, cli

from . import __version__, tei
from .engines import ENGINE_NAMES, build_engine

REGISTRY_PATH = Path(__file__).resolve().parent.parent / "models.yaml"

WORKER = Worker(
    name="inferences-embedding",
    version=__version__,
    engines=ENGINE_NAMES,
    build_engine=build_engine,
    registry_path=REGISTRY_PATH,
    prog="embedding_worker",
    log_level_env="EMBEDDING_LOG_LEVEL",
    routes=tei.routes(),
)


def main(argv: list[str] | None = None) -> int:
    return cli.main(WORKER, argv)


if __name__ == "__main__":
    sys.exit(main())
