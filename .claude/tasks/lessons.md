# Lessons

Long-term memory for this repo. One lesson per entry: the rule as an imperative, then the
reason it exists. Each of these came from a correction Dhira made — keep it that way, and
keep it short enough to stay read.

Not a diary. When a lesson stops being true, edit it; do not append a contradiction.

---

### Never commit model weights

Pin them in a `models.yaml` manifest — repo, immutable revision, sha256, size — and fetch at
runtime into a gitignored mounted directory.

**Why:** weights are hundreds of megabytes and GitHub refuses files over 100 MB. A manifest
gives reproducibility without the bytes, and an unverifiable digest can then make a model
unloadable instead of silently trusted.

### Keep one source of truth for dependencies

Never let `requirements.txt` and `pyproject.toml` describe the same thing.

**Why:** two files with the same authority disagree eventually, and the one the build reads
is not always the one a human edits. Bounds live in `pyproject.toml`, exact versions in the
single root `uv.lock`, and the image build generates its input from the lock.

### Make comments earn their place

One or two lines where a comment prevents a real mistake. History, restated obviousness and
multi-sentence rationale go to the README or the technical requirement.

**Why:** the root `pyproject.toml` reached 61% comments (16 of 26 lines) and had to be cut.
Narration crowds out the two lines that actually matter, so nobody reads either.

### Test the logic, not just the plumbing

Coverage per module, floor per service, and mutation as the standard of evidence.

**Why:** 54 tests passed while all three engine adapters sat at 0%. Sockets, framing and
locking were tested hard; the OCR itself was not tested at all. A green suite proved nothing
about the thing the service exists to do.

### Put shared code in `packages/`, never copy it

Two services needing the same thing means it moves, not that it is duplicated.

**Why:** copies drift silently, and the drift is only discovered when the two ends of a
protocol stop agreeing. `packages/golibs/` and `packages/pylibs/` exist for this.

### Pin exact versions, not rolling tags

Exact patch for the interpreter, the base image and the toolchain.

**Why:** a rolling tag changes what you shipped without changing anything you wrote. Debian
renaming `libglib2.0-0` to `libglib2.0-0t64` between releases is the concrete case: an
unpinned base would have broken the image on somebody else's schedule.

### One tool per job

Do not keep a second mechanism that does what an existing one already does.

**Why:** a weekly workflow existed only because Dependabot could not see transitive packages
without a lockfile. Once the lock existed, keeping both meant two things reporting on the
same dependencies and neither being owned.

### A readiness check must test the mechanism actually in use

Check the thing the repo runs, not the ambient equivalent.

**Why:** `make doctor` read `python3` off `PATH` and failed a machine that was entirely
ready, because uv provisions the interpreter here and asdf has no python plugin installed. A
check that fails a working machine is a broken check, and its advice was to compile CPython
for nothing.

### Refuse undeclared control fields rather than ignoring them

Each op declares the fields it accepts; anything else is a `bad_request`.

**Why:** `infer` silently ignored a `model` field, so a caller naming a model got whatever
happened to be resident — the wrong answer to a reasonable question. Silent tolerance hides
typos and turns a client bug into a server behaviour.
