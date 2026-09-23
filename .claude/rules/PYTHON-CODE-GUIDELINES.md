# Python code guidelines

These are the rules this project has already paid for. Follow them; argue with them in a PR,
not in a diff.

## Dependencies

- **One lock, at the repo root.** `uv.lock` covers every workspace member. Never add a
  per-service lock: a workspace resolves as one set, and a second lockfile gives two files
  the authority to disagree.
- **One source of truth per dependency.** Bounds live in a service's `pyproject.toml`.
  Nothing else describes the same dependency — no `requirements.txt` beside a `pyproject`,
  no pinned copy in a Dockerfile. The image build generates its input from the lock.
- **Services are applications**, so they set `[tool.uv] package = false` and appear in the
  lock as `virtual`. Only real shared code under `packages/pylibs/` is installable.
- **A workspace dependency is declared by name** plus
  `[tool.uv.sources] <name> = { workspace = true }`. It resolves into the same root lock as
  `editable`, so edits are live and there is no version to pin.
- **Add a dependency only when the standard library will not do.** Every one is a supply
  chain, a wheel that may not exist for the next Python, and an upgrade someone has to
  review. Say in the PR what it buys.
- **Pin exactly where the runtime is concerned.** `requires-python` states the floor; the
  interpreter and the base image are pinned to an exact patch, never a rolling tag.

## Layout

- A service owns `services/<name>/`: its `pyproject.toml`, `Makefile`, `Dockerfile`, tests,
  README and source. Nothing at the repo root accumulates service detail.
- Shared Python lives in `packages/pylibs/<name>/` with a `src/` layout, so tests run against
  the installed package rather than whatever happens to be on `sys.path`.
- Copying code between services is how two implementations drift. If two services need it,
  it belongs in `packages/pylibs/`.

## Style

- **Type hints on anything public**, and on anything where the shape is not obvious from the
  name. They are documentation that cannot go stale silently.
- **Docstrings say why**, not what. A module docstring explaining a non-obvious boundary
  earns its place; one narrating what the next line does does not.
- Match the file you are in. Consistency beats a better idea introduced halfway down a file.
- Exceptions carry a code and a message: the code is the contract, the message is for humans.
  Never let a bare `except` swallow something a caller needed to know.

## Comments

A comment exists to prevent a mistake. Nothing else earns its place.

Keep: a non-obvious why (a footgun, a spec quirk, a limit that would surprise a reader); the
contract a caller cannot infer from the code (what it guarantees, what it refuses); a build
tag or a generated-file marker.

Cut: what the code already says; how the file got here, what was tried, what went wrong
before; a restated signature, field name or JSON key; section banners; anything a reviewer
would call narration.

Where the cut text belongs instead: the pull request description, `docs/`, or
`.claude/tasks/lessons.md`. Not the source.

The bar: a reader should find the file comparable to the Go standard library, which is close
to comment-free. As a smell test, comment lines stay well under 10% of a source file; above
that, the file is usually carrying prose that belongs somewhere else.

## Tests

- **Table-driven with `self.subTest(name)`** for case expansion: one row per case, so a
  failure names the row and a new case is one line.
- **Plain methods for scenarios** that need their own threads, fixtures or teardown. Forcing
  those into a table puts the setup flags in the fixture and the branches in the loop.
- **Stdlib only, offline, deterministic.** No network, no model weights, no sleeps, no system
  binaries. Patch the seam instead: `subprocess.run`, `urlopen`, a session object.
- **Coverage has a floor per service**, enforced by that service's `make coverage`. The floor
  is a tripwire, not a target — read which lines are covered, not the percentage.
- The test that matters covers the logic. A suite can be green with every adapter at 0%.
