"""Parsing of models.yaml into the shapes the rest of the worker uses."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

DEFAULT_REGISTRY_PATH = Path(__file__).resolve().parent.parent / "models.yaml"

# An id becomes a directory name under $MODELS_DIR, so it has to be one safe path
# segment -- the same guarantee `dest` already gets.
VALID_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class RegistryError(Exception):
    """models.yaml is missing, malformed, or names something we cannot honour."""


@dataclass(frozen=True)
class ModelFile:
    """One file to materialise under $MODELS_DIR/<model id>/."""

    repo: str
    revision: str
    path: str
    dest: str
    sha256: Optional[str]
    bytes: Optional[int]

    @property
    def verified(self) -> bool:
        return bool(self.sha256)


@dataclass(frozen=True)
class ModelSpec:
    id: str
    description: str
    engine: str
    langs: List[str]
    source_type: str
    files: List[ModelFile] = field(default_factory=list)
    options: Dict[str, Any] = field(default_factory=dict)
    apt_packages: List[str] = field(default_factory=list)

    @property
    def needs_download(self) -> bool:
        return self.source_type == "huggingface" and bool(self.files)

    def summary(self) -> Dict[str, Any]:
        """The public view of a model, as returned by the `list` op."""
        return {
            "id": self.id,
            "description": " ".join(self.description.split()),
            "engine": self.engine,
            "langs": list(self.langs),
            "source_type": self.source_type,
            "files": len(self.files),
            "bytes": sum(f.bytes or 0 for f in self.files),
            "unverified_files": [f.dest for f in self.files if not f.verified],
        }


@dataclass(frozen=True)
class Registry:
    version: int
    default_model: str
    models: Dict[str, ModelSpec]

    def get(self, model_id: str) -> ModelSpec:
        try:
            return self.models[model_id]
        except KeyError:
            known = ", ".join(sorted(self.models)) or "<none>"
            raise RegistryError(f"unknown model '{model_id}'; registry has: {known}") from None

    def summaries(self) -> List[Dict[str, Any]]:
        return [self.models[k].summary() for k in self.models]


def load_registry(path: Path = DEFAULT_REGISTRY_PATH) -> Registry:
    if not path.is_file():
        raise RegistryError(f"registry not found at {path}")

    with path.open(encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    if not isinstance(raw, dict):
        raise RegistryError(f"{path} must contain a mapping at the top level")

    entries = raw.get("models") or []
    if not entries:
        raise RegistryError(f"{path} declares no models")

    models: Dict[str, ModelSpec] = {}
    for entry in entries:
        spec = _parse_model(entry, path)
        if spec.id in models:
            raise RegistryError(f"duplicate model id '{spec.id}' in {path}")
        models[spec.id] = spec

    default_model = raw.get("default_model")
    if default_model is not None and default_model not in models:
        raise RegistryError(f"default_model '{default_model}' is not one of the declared models")

    return Registry(
        version=int(raw.get("version", 1)),
        default_model=default_model or next(iter(models)),
        models=models,
    )


def _parse_model(entry: Any, path: Path) -> ModelSpec:
    if not isinstance(entry, dict):
        raise RegistryError(f"each entry under models: in {path} must be a mapping")

    for required in ("id", "engine"):
        if not entry.get(required):
            raise RegistryError(f"a model entry in {path} is missing '{required}'")

    if not VALID_ID.match(str(entry["id"])):
        raise RegistryError(
            f"model id {entry['id']!r} in {path} is not a safe directory name; "
            "use letters, digits, dot, dash or underscore"
        )

    source = entry.get("source") or {}
    source_type = source.get("type")
    if source_type not in ("huggingface", "system"):
        raise RegistryError(
            f"model '{entry['id']}' has source.type '{source_type}'; expected huggingface or system"
        )

    files = [
        _parse_file(raw_file, entry["id"], source, path) for raw_file in (entry.get("files") or [])
    ]
    if source_type == "huggingface" and not files:
        raise RegistryError(f"model '{entry['id']}' is a huggingface source but lists no files")

    return ModelSpec(
        id=entry["id"],
        description=entry.get("description", ""),
        engine=entry["engine"],
        langs=list(entry.get("langs") or []),
        source_type=source_type,
        files=files,
        options=dict(entry.get("options") or {}),
        apt_packages=list(source.get("apt_packages") or []),
    )


def _parse_file(raw: Any, model_id: str, source: Dict[str, Any], path: Path) -> ModelFile:
    if not isinstance(raw, dict) or not raw.get("path"):
        raise RegistryError(f"model '{model_id}' in {path} has a file entry without a 'path'")

    repo = raw.get("repo") or source.get("repo")
    revision = raw.get("revision") or source.get("revision")
    if not repo or not revision:
        raise RegistryError(
            f"file '{raw['path']}' of model '{model_id}' needs a repo and an immutable revision"
        )

    dest = raw.get("dest") or raw["path"]
    if Path(dest).is_absolute() or ".." in Path(dest).parts:
        raise RegistryError(f"file dest '{dest}' of model '{model_id}' must stay inside the model dir")

    return ModelFile(
        repo=repo,
        revision=revision,
        path=raw["path"],
        dest=dest,
        sha256=raw.get("sha256"),
        bytes=raw.get("bytes"),
    )
