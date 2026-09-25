---
type: technical-design
status: in-review
owner: Dhira Wigata
product: homelab
prd: — (Rin's §4 of `laya-and-laya-dashboard-requirements.md`, folded into Dita's requirement)
date: 2026-09-24
tags:
  - dita
  - inferences
  - dashboard
  - technical-design
---

# Technical Design — services/inferences-dashboard

> The requirement is Dita's draft v1 (`second-brain/ai/dita/specs/inferences-dashboard-requirements.md`),
> followed rather than redesigned; this is where it lands once read, with what was built and every
> decision it left open marked as decided or still open. Two house departures are the requirement's own
> and stand: `product: homelab`, and no templ + HTMX, frozen-DB or strangler-fig rules — this is a static
> SPA over an existing REST API.

## Context

The decision worker (`inferences-system-one`) answers typed questions with per-option probabilities, and
nothing on this box records what a human did with those answers. Of 86 alerts over 14 days, all 86 have
`reported = 0`: there are effectively no labels, so nothing can be trained or evaluated, and the base
checkpoints sit below the majority-class baseline (0.362 vs 0.461). The gap is not a nicer UI. It is that
the first useful dataset does not exist, and the cheap way to get it is to capture the decision a human
makes when a model suggests one.

**The worker is not merged.** Rin's branch `feat/inferences-system-one` has no commits beyond `main`; her
worker exists only in her worktree, which this change did not touch. So the dashboard is built against a
**documented stub**, labelled as one everywhere it appears, and the one piece of code that knows the
worker's answer shape is an adapter that changes when the worker lands.

## Goals and non-goals

**Goals** (the requirement's, unchanged)

1. Run a decision by hand: paste state text, pick a schema, see **per-option probabilities** and
   confidence — never a bare single value.
2. **Schemas as named, versioned templates.** Saving writes a new version; a version is never mutated.
3. Capture **accepted vs corrected** for every recommendation as an **append-only pair**.
4. Evaluate: a labelled CSV gives accuracy, per-class breakdown and **Brier / ECE against the
   majority-class baseline**.

**Non-goals** (unchanged): no auto-submission into any regulated field; no training, no registry UI; not a
second front end; ≤20 options per question; **no LAN exposure before auth**; not the decision of record.
Out of v1 by the requirement: file upload and OCR (pasted text only).

## Design

```
browser (SPA)  ──/api/inferences/*──▶  Caddy (dist/ + same-origin proxy, 127.0.0.1:8443)
                                          │
                                          ▼
                                   orchestrator REST (Go, :2104)
                                   ├── gateway routes (PR #14)  ──▶ workers on `proxy`
                                   └── decisions routes (this change)
                                        │  store: append-only JSONL under DECISIONS_DIR
                                        └──▶ inferences-system-one  POST /decide
```

The SPA never talks to a worker and never holds a credential. Caddy proxies only `/api/inferences/*`;
every other `/api` path is 404 on this origin, so `/api/v1/chat`, which spends an LLM key, is not reachable
from the dashboard.

### Data model

The requirement's three stores, kept deliberately different.

**1. Schema templates** — `templates.jsonl`, one line per version. A draft is `{name, description,
questions[]}`; a question is `{name, type: noul | choice | score, options[2..20], criteria, range?}`. The
version is assigned by the store and is the next integer; there is no update path. A version is withdrawn from
new decisions by a line in `retirements.jsonl` (`{name, version, retired_at}`, once per version), never by
touching `templates.jsonl`; every listed template carries `retired`. On Templates, loading a template lists its
versions and each can be retired after a confirmation that says Decide will stop offering it; a retired version
is shown as retired in the list and disabled on Decide. The dashboard defaults to the first version the server
marks `usable`, says why a version cannot be used, and marks one that `usable` but has `authoring_issues` as
needing an update rather than blocking it — it displays the server's verdict, it does not compute its own.
**2. Prediction + correction** — two files, two writes, never merged:

| field | where | notes |
| --- | --- | --- |
| `id` | prediction | 24 hex characters, random |
| `created_at` | prediction | server clock, UTC |
| `schema_id`, `schema_version` | prediction | what was asked |
| `input_text` | prediction | as submitted |
| `model_id`, `model_revision` | prediction | as the worker reported them |
| `prediction` | prediction | the worker's answer **as returned** (content; JSON whitespace compacted) |
| `confidence` | prediction | per question, as returned, never recomputed |
| `answers` | prediction | the reply as `ParseReply` read it **at write time**; see "History is read under the rules it was written under" |
| `corrected_at` | correction | the second write |
| `correction` (`answers`) | correction | the human's answer per question, a **separate record** |
| `outcomes` | correction | `accepted` / `corrected` per question, derived by the store from the stored prediction, never taken from the caller |

**3. Metrics** — `GET /api/inferences/stats`: decision outcomes since start (`ok`, `no_model`,
`schema_invalid`, `engine_refused`, `not_running`, `busy`, `timeout`, `bad_reply`) and, per schema version,
predictions, corrected, answers accepted and answers corrected. In memory and derived; nothing payload-bearing.

**Why files, not a database.** CGO-free and stdlib-only was the constraint, one orchestrator owns the store,
and append-only is a property a file with `O_APPEND` and an fsync per line holds by construction. A store
directory carries a `FORMAT` marker (`dita-decisions/1`, the "migration"): an empty directory is initialised,
a different marker is refused. On start every line is replayed; in the rules files (templates, retirements) a line that does not parse,
a template version out of sequence, or a retirement of a version never written or retired twice **stops the
orchestrator** rather than being repaired. Record files are quarantined instead; see "A damaged record costs
that record". `retirements.jsonl` arrived under the same marker: it
only adds, and a build that predates it ignores it, so a rollback makes retired versions selectable again.

### The worker contract (stub-defined)

The requirement's open question is the worker's answer shape, and the dashboard must not depend on its
resolution. `services/dita-orchestrator/decisions/worker.go` is the only place that knows it:

```
POST {INFERENCES_SYSTEM_ONE_URL}/decide
  {"text": "...", "questions": [{"name", "type", "options", "range"?, "criteria"?}]}
200
  {"model_id": "...", "model_revision": "...",
   "answers": [{"name": "severity", "probabilities": {"low": 0.2, ...}, "confidence": 0.7}]}
```

An answer is refused (502, nothing written) unless it answers exactly the questions asked, scores exactly
each question's options with probabilities in [0, 1], and carries a confidence. The dashboard reads the
orchestrator's normalised view, `answers[{question, type, options[{option, probability}] best first,
confidence}]`, so when the real worker lands only the adapter changes.

### Interfaces — SPA to orchestrator, all under `/api/inferences`

| route | purpose |
| --- | --- |
| `GET /schemas` · `POST /schemas` | latest version of each template · save a new version |
| `GET /schemas/{name}/versions[/{v}]` | every version · one version, each with `retired` |
| `POST /schemas/{name}/versions/{v}/retire` | → `200 {name, version, retired: true, retired_at}`; retiring again returns the first || `POST /decisions` | `{text, schema:{name, version}}` → ask the worker, then write the prediction |
| `GET /decisions?limit=N` · `GET /decisions/{id}` | recent, newest first · one, with its correction |
| `POST /decisions/{id}/correction` | `{answers:{question: option}}` → 201 once; **409** the second time |
| `POST /evaluations` · `GET /evaluations` | score a labelled set and store it · list runs |
| `GET /stats` | the capture path's health |

These are **new Go routes in the orchestrator, added in this change**; the SPA holds no logic the server does
not also enforce.

**Status codes.** `503` from the worker itself means no model is resident and renders "worker has no model
loaded"; the gateway's `503` with `reason: not_running` or `busy` renders as such. `400` for a malformed
schema or input, with zod-style `issues[{path, message}]`; `422` for an engine refusal; `409` for a second
correction; `404`; `502` for an answer to a different question; `504` for a timeout. **No 4xx is retried; no
write is retried at all** (a retried decide would be a second prediction).

### Validation, once in each language, proven equal

The rules exist twice by necessity — the client for UX, the server because a hand-rolled request must not
bypass them. `specs/decisions/schema-cases.json` holds 41 template cases and 7 text cases (valid templates at every limit, each rule
broken alone, several broken at once) with the exact issue paths expected. The Go suite and the zod suite
both run the file; a rule that drifts in either fails its suite. The zod schema keeps its rules in one
`superRefine` over loosely-typed fields, because zod 4 skips later refinements after a failed field check and
would then report fewer faults than Go does.

A `noul` question's options are exactly `false` and `true`, in either order: the worker answers a noul with
those keys and refuses one asked with any others, so a `yes`/`no` template would save and then fail at every
`/decide`. `ValidateDraft`, zod and `schema-cases.json` all carry the rule.

**Status codes added by the hardening change.** Every `POST` answers `403` (`Forbidden`) when the browser marks it
`Sec-Fetch-Site: cross-site`, `415` (`Validation`) without `Content-Type: application/json`, and `401`
(`Unauthorized`) without the token when `INFERENCES_API_TOKEN` is set (gateway doc, "Writes"). A decision on a
retired version is `410` (`Retired`), naming it. A decision on a version the list marks `usable: false` is
`400` (`Validation`) with its `faults` as `issues`, answered before any worker call. A second evaluation while
one is being scored is `429` (`Overloaded`). A correction of a prediction whose answers cannot be read is `422`
(`Unreadable`). Every error, a recovered panic included, is `{error, error_type}`: the shape every route and
every worker already spoke, so the dashboard parses one; the `problem+json` that `w-tools` writes by default is
replaced through `RecoverConfig.ErrorWriter`.

**One owner for "can this template be used".** `GET /schemas`, `…/versions` and `…/versions/{v}` return, per
version, `usable` and `faults` (`[{path, message}]`, from `decisions.CheckQuestions`, the runtime rules the
worker applies), `retired`, and `authoring_issues` (what `ValidateDraft` says, for the editor only). `Decide`
refuses on exactly that `usable`, so what the list promises is what the call does. The two rule sets stay
separate on purpose: a template saved before an authoring rule existed, like the live `alert-triage` v2 with no
criteria, has authoring issues and still runs. The dashboard displays the server's verdict rather than computing
its own. `CheckQuestions` and the worker's `parse_decide` are held to the same first fault's path and
`error_type` by `specs/decisions/worker-cases.json`; their prose is not compared.

**History is read under the rules it was written under.** A prediction stores its parsed answers beside the raw
reply, so tightening `ParseReply` later cannot empty history. A record written before that field is parsed
under today's rules, and the view says `answers_reparsed: true`; if today's rules refuse it, the view carries
`answers_error` with `answers: null`, the list still answers, and a correction of it is `422`.

**A damaged record costs that record, not the service.** In `predictions.jsonl`, `corrections.jsonl` and
`evaluations.jsonl` (which takes uploads), a line
that cannot be read at start (not JSON, longer than a stored line may be, a template version never written, a
duplicate id, a correction whose prediction is absent, a second correction) is **quarantined**: its exact bytes
are appended to `<file>.rejected` (once, however often the store reopens), one Error line names file, line and
reason, the record is skipped, and the lines after it are read. `GET /stats` carries `quarantined` per file, and
start logs `N lines quarantined; see <file>.rejected`. A last line with no newline, a torn append, is set aside
the same way and ended, so the next record starts on its own line. `templates.jsonl` and `retirements.jsonl`
stay **fatal**: a template misread silently becomes the rules the whole history is read under, and the file is
tiny and written a handful of times, so correctness beats availability there. For records, availability beats
correctness: the volume and the uploads are there, and a dropped line is one record, visibly reported.

**Bounds.** A stored line is at most `MaxRecord` (16 MiB): the reader's buffer and the writer's refusal are the
same constant, and the store writes JSON without HTML escaping, so `<` costs one byte, not six. A write or sync
that fails is truncated back; if that fails too, the store takes no more writes until a restart re-reads it.
An evaluation's name is 1-200 characters and each class 1-100; its rows are decoded one at a time and the
upload is refused at the first row past 10 000; one is scored at a time. A history page is at most 50.

Every new question carries `criteria`, non-blank, at most 500 characters. It is what the model reads as the
question's instructions; without it the model sees only the question's name, and the answer moves materially
(measured through the orchestrator: `warning` 0.8507 / confidence 0.5800 with criteria, 0.6396 / 0.3194
without). Templates stored before the rule still run: the worker falls back to the name. A `score` question's
options are plain decimals (`-1`, `0.5`, `3`; no exponent, hex or infinity, which Go, zod and Python read
differently), strictly rising, and inside its `range` when it has one. The editor has **no input for it yet**: loading a template and saving
the next version keeps its criteria, but a new template cannot be given any from the dashboard.
### Control flow

Happy path: paste → pick a template version (the first usable one is preselected) → **Decide** → the worker answers → the prediction is written →
the page moves to `/decisions/{id}` → the answer renders as a suggestion → the human picks each answer
(nothing is preselected; **use suggestion** is a click) → **Record answer** → the correction is written and
shown in place of the form. The URL carries the prediction id, so a reload reads the pair back from the store.

Failure modes, each tested:

- **No model resident** — the worker's 503 is passed through unchanged; nothing is written.
- **Worker not running** — the gateway's 503 (`not_running`); nothing is written.
- **Schema invalid** — zod refuses before the call; the server refuses the same shapes with 400.
- **An answer to a different question** — 502; nothing is written.
- **A second correction** — 409, the first stands, on disk and on screen.
- **A correction after the template changed** — checked against the version the prediction was made
  under, not the newest.
- **A stored template the rules refuse** — marked on Decide with its reason, never the default, and refused
  on the page before any request.
- **A path the dashboard does not have** (`/history`; History is `/decisions`) — a catch-all inside the
  layout says there is no page there and links back to Decide, instead of the router's error screen.

### The recommendation contract

Every model value renders as a suggestion ("Rekomendasi · suggestion"), in a visually distinct card, with the
**top two alternatives and the confidence**, then every option's probability as an inline SVG bar. It is never
auto-submitted: the correction form starts empty.

### Eval

The CSV is one row per labelled example: a `label` column and one `p:<option>` column per option holding the
model's probability. The browser parses it (RFC 4180 quoting, BOM, CRLF); the orchestrator validates the rows
(same classes per row, label among them, probabilities in [0, 1] summing to 1 ± 0.02) and computes, storing
the run:

- accuracy (argmax), per-class support, predicted, correct, precision, recall;
- multi-class Brier, `mean Σ_k (p_k − y_k)²`;
- ECE over 10 equal-width bins of top-class confidence;
- the **majority-class baseline**: always the most frequent label, with the label frequencies as its
  probabilities — accuracy = majority share, Brier = `1 − Σ q_k²`, ECE 0 by construction.

`beats_baseline` needs both better accuracy and lower Brier; the panel always shows the model beside the
baseline, never alone.

Above it, a **calibration panel** states that no calibration is fitted: every temperature in the checkpoint
is 1.0, so temperature scaling is the identity and the probabilities are the model's raw softmax. The text is
static; it changes when a fitted checkpoint lands, not before. `act_probability` is not shown anywhere: it is
1.0 on this checkpoint and carries no signal.

## Alternatives considered

The requirement's four stand as written: no orchestrator-embedded UI, no notebook, no TanStack Table or
Recharts in v1, and no DIP `infer` change. Decided here:

- **SQLite for the store.** Rejected: `mattn/go-sqlite3` is CGO, `modernc.org/sqlite` is a large dependency,
  and neither is needed for an append-only log that one process owns.
- **`@hookform/resolvers`.** Dropped: a twenty-line resolver maps zod's issues onto the editor's fields.
- **Computing eval metrics in the browser.** Rejected: the stored result would then be whatever a client said
  it was. The browser parses; the server computes and stores.
- **Playwright's route mocking for the e2e.** Rejected: the scenario exists to catch a lost pair, which a
  mocked backend cannot lose. The e2e runs the real orchestrator over a real store.

## Decisions on the requirement's open questions

- **Service and directory name** — `inferences-dashboard`, `docs/inferences/dashboard/`, as recommended: a
  task name, so renaming the worker does not touch it.
- **Build location, lockfile, `.tool-versions`** — the app lives in `services/inferences-dashboard/`,
  `pnpm-lock.yaml` beside it, and **nodejs 26.7.0 and pnpm 12.6.0 join `.tool-versions`**, checked by
  `make doctor`. The root Makefile classifies units by marker — `go.mod`, `package.json`, `pyproject.toml` —
  and a unit with none stops `make` with its name, replacing "no `go.mod` means Python".
- **Still open, Dhira's:** the DIP response shape (not assumed), what "user" means (the gate on a hostname),
  retention and access for the correction store.

## Migration and rollout

The store is created by the orchestrator on first start in `DECISIONS_DIR` (`/data/decisions`, a named
volume `dita-decisions` in `services/dita-orchestrator/compose.yaml`). **No seed rows**: the first write is a
real prediction. The SPA ships as static files behind `services/inferences-dashboard/Caddyfile`, run by
`compose.yaml` as `caddy:2.11.4` on `proxy`, published on **127.0.0.1:8443 only**. **What is deployed goes
further:** `inferences.home.arpa` and `orchestrator.api.home.arpa` are both live on the LAN through the box's
own Caddy, whose site block is copied from this `Caddyfile` by hand (and has drifted: it omits
`Permissions-Policy`). That is against this design's non-goal of no LAN exposure before auth; see Security.

## Rollback

`make down` and remove the static directory: the SPA holds nothing. The store is **never** dropped as part of
a rollback; the named volume survives `compose down` and only `down -v` would remove it.

## Observability

`GET /api/inferences/stats` carries the requirement's counts: outcome per request, and correction rate per
schema version. **Alert on a correction rate of zero for a template that has predictions** — the capture path
is then broken, which is otherwise silent. The five-minute check is the e2e scenario by hand: submit a known
schema, see probabilities, record a correction, read the row back with both halves.

Not built: latency p50/p95 at the proxy, and a calibration drift line per template. Both need a metrics
backend the dashboard does not have yet.

## Security and privacy

- **Nothing can be erased, by decision.** The store keeps each prediction's `input_text` forever, and there is
  no delete: append-only is what makes the training pairs trustworthy. That is acceptable for homelab alerts. It
  matters the day someone pastes an alert that contains a credential: the only remedy is to stop the
  orchestrator and edit the volume by hand, which `Open` will then check line by line.
- **The container runs as root, by decision.** `USER 65532` is cheap on a fresh volume, but the deployed
  `dita-decisions` named volume already holds root-owned `0640` files, so the image change alone would crash-loop
  the gateway on permission denied. Doing it properly needs a one-time `chown -R 65532:65532` of that volume at
  the same deploy, and a rollback plan for it. That is Dhira's call; until then the process stays root inside a
  `FROM scratch` image with no shell.

- **No secrets in the SPA.** `scripts/check-dist.mjs` greps `dist/` for key shapes (private key blocks,
  AWS, GitHub, Hugging Face, `sk-` style, Slack, Google, JWT, assigned secrets) and fails the build; shown to
  exit 1 on two planted keys.
- **CSP from Caddy** — `default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; font-src
  'self'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'`
  — with no `unsafe-*`, plus HSTS, `nosniff`, `Referrer-Policy: no-referrer`, a locked-down
  `Permissions-Policy`, COOP and CORP. The build emits no inline script or style (`assetsInlineLimit: 0`, no
  module-preload polyfill); the e2e asserts zero console errors, so a CSP violation would fail it.
- **ESLint enforces the rendering rules**: `dangerouslySetInnerHTML`, `innerHTML`/`outerHTML`, `eval`,
  `new Function` and `localStorage` are errors.
- **Auth does not exist, and both origins are on the LAN** (`inferences.home.arpa`,
  `orchestrator.api.home.arpa`). The session cookie, login route and store are new orchestrator work and need
  the definition of "user". Keeping LAN users out belongs in Caddy (`basic_auth` or `forward_auth` on the
  site block); a token header the proxy injects authenticates the proxy, not the person, and would ride on
  cross-site requests too, so it is not used. Cross-site writes are refused by the orchestrator instead
  (JSON-only mutating routes, `Sec-Fetch-Site: cross-site` refused); the dashboard already sends JSON, and
  shows a 401, 403 or 415 on a write as a sentence saying nothing was changed.
- **Two failure shapes are read.** Every route answers `{error, error_type}` and a recovered panic answers
  RFC 9457 `problem+json`; `toProblem` takes the `detail` (or `title`) of the second, so neither reaches the
  reader as raw JSON. A stored prediction whose reply no longer parses (`answers: null`) is shown as such,
  with no correction form, instead of breaking the page.
- `pnpm audit --prod`: no known vulnerabilities. The build writes a **CycloneDX 1.7 SBOM** of the production
  dependencies with pnpm's own `sbom` (not served; it sits beside `dist/`).
- Nothing the model returns is stored as a fact about a customer: a prediction is a **suggestion event** —
  model revision, input, probabilities, and the human's answer.
- **pnpm's minimum release age** refused `prettier@3.9.9` (published hours earlier); the pin is 3.9.8 rather
  than an exemption. pnpm reports `eslint@9.39.5` **deprecated** in favour of 10; the requirement pins ESLint 9,
  so it stays, and the major bump is a deliberate PR.

## Testing

- **Unit (Vitest + React Testing Library), 150 tests:** the 26 shared schema cases; the noul rule, including the `yes`/`no` it used to accept; criteria required, with its input, help text and error in the editor; score options numeric, rising and within their range; the server's usable verdict followed on Decide (an unusable version never preselected, shown with the server's fault and refused before any request; a usable one with authoring issues selectable and marked, and its issues marked at the editor's fields), with the editor's rules as the fallback for an older orchestrator; both failure shapes read; 401, 403 and 415 as sentences; a stored answer that no longer parses; retired versions disabled on Decide, and retired on Templates only after a confirmation; the catch-all for an unknown path; the calibration panel; the probability renderer
  (top-2 + confidence, never a bare value); correction state transitions (nothing preselected, record, 409
  shown and not retried, a stored correction shown on load); the CSV parser; error mapping per status; no
  retry on 4xx; the eval panel beside its baseline; the worker strip surviving a worker that is not running
  and the gateway failing. Coverage floors 92 / 84 / 91 / 93 (statements / branches / functions / lines), just
  under what the suite reaches.
- **Server (Go), against the real store:** the append-only refusal of a second correction, read back after
  reopening the directory; nothing written when the worker refuses (503, 400, 422), is not running, or answers
  a different question; a correction checked against its own schema version; a corrupted store refused at
  start; the evaluation metrics worked by hand. 15 of 16 deliberate breakages caught; the survivor is the
  per-line `fsync`, which no unit test can observe.
- **One Playwright e2e** — paste → decide → correct → reload → the correction is still attached — against
  Caddy with the production Caddyfile, the orchestrator binary over a fresh store, and the stub. It also reads
  the pair back through the API (the correction beside an unchanged prediction), shows the row in History, and
  shows a second correction refused with the first standing. A fourth test opens `/history` through Caddy and
  follows the catch-all back to Decide.
- **The same e2e against the real worker**, when `E2E_SYSTEM_ONE_URL` names one. The worker publishes no host
  port, so the URL is its address on `proxy`:

  ```sh
  E2E_SYSTEM_ONE_URL=http://$(docker inspect -f '{{(index .NetworkSettings.Networks "proxy").IPAddress}}' \
    inferences-system-one):8080 make -C services/inferences-dashboard e2e
  ```

  No stub starts. The run fails, never falls back, if the worker is unreachable or not ready. It also asserts
  what only a real answer shows: `/info` and the stored prediction carry the `model_id` and `model_revision`
  that `services/inferences-system-one/models.yaml` pins (`E2E_SYSTEM_ONE_MODELS` overrides the path), the
  revision is on the page, every answer's probabilities sum to 1 within 1e-6, the noul answers with `false`
  and `true`, and no page shows a stub label.
- Not covered: model quality, which is the eval panel's number rather than a test.

## Evidence

Under [`evidence/`](evidence/). A `-stubbed` screenshot shows the stub worker and says so on the page, in
red; a `-real` one is written by the real-worker run. Either eval screenshot uses a synthetic CSV and says so.

| file | what |
| --- | --- |
| `1-answer-view-{stubbed,real}.png` | per-option probabilities, top-2 and confidence for a three-question schema |
| `2-correction-captured-{stubbed,real}.png` | the recorded pair, accepted and corrected per question |
| `3-eval-panel-{stubbed,real}.png` | the calibration panel; a synthetic labelled CSV beside the majority-class baseline |
| `e2e-transcript.txt` | the Playwright run |
| `headers-and-ports.txt` | `curl -skI` of the served page and `ss -ltn` |

## Open questions

- [ ] **Q:** ratify or reject the DIP response-shape change. Not assumed here. — *owner:* Dhira
- [ ] **Q:** what "user" means on a single-account box: the gate on auth, and on a hostname. — *owner:* Dhira
- [ ] **Q:** retention and access for the correction store: personal data and the training set. — *owner:* Dhira
- [x] **Follow-up:** an input for `criteria` in the template editor, since it changes the answer. Done, and
  required. — *owner:* Dita
- [ ] **When the worker lands:** replace the stub contract in `decisions/worker.go` with the real answer shape
  (the worker's PR). The e2e's real-worker path exists; run it with `E2E_SYSTEM_ONE_URL`.