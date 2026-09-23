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
questions[]}`; a question is `{name, type: noul | choice | score, options[2..20], range?}`. The version is
assigned by the store and is the next integer; there is no update path.

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
| `corrected_at` | correction | the second write |
| `correction` (`answers`) | correction | the human's answer per question, a **separate record** |
| `outcomes` | correction | `accepted` / `corrected` per question, derived by the store from the stored prediction, never taken from the caller |

**3. Metrics** — `GET /api/inferences/stats`: decision outcomes since start (`ok`, `no_model`,
`schema_invalid`, `engine_refused`, `not_running`, `busy`, `timeout`, `bad_reply`) and, per schema version,
predictions, corrected, answers accepted and answers corrected. In memory and derived; nothing payload-bearing.

**Why files, not a database.** CGO-free and stdlib-only was the constraint, one orchestrator owns the store,
and append-only is a property a file with `O_APPEND` and an fsync per line holds by construction. A store
directory carries a `FORMAT` marker (`dita-decisions/1`, the "migration"): an empty directory is initialised,
a different marker is refused. On start every line is replayed; a line that does not parse, a template version
out of sequence, or one prediction corrected twice **stops the orchestrator** rather than being repaired.

### The worker contract (stub-defined)

The requirement's open question is the worker's answer shape, and the dashboard must not depend on its
resolution. `services/dita-orchestrator/decisions/worker.go` is the only place that knows it:

```
POST {INFERENCES_SYSTEM_ONE_URL}/decide
  {"text": "...", "questions": [{"name", "type", "options", "range"?}]}
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
| `GET /schemas/{name}/versions[/{v}]` | every version · one version |
| `POST /decisions` | `{text, schema:{name, version}}` → ask the worker, then write the prediction |
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
bypass them. `specs/decisions/schema-cases.json` holds 22 cases (valid templates at every limit, each rule
broken alone, several broken at once) with the exact issue paths expected. The Go suite and the zod suite
both run the file; a rule that drifts in either fails its suite. The zod schema keeps its rules in one
`superRefine` over loosely-typed fields, because zod 4 skips later refinements after a failed field check and
would then report fewer faults than Go does.

### Control flow

Happy path: paste → pick a template version → **Decide** → the worker answers → the prediction is written →
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
`compose.yaml` as `caddy:2.11.4` on `proxy`, published on **127.0.0.1:8443 only**. **No LAN hostname** until
the orchestrator has auth.

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
- **Auth does not exist.** The session cookie, login route and store are new orchestrator work and need the
  definition of "user". Until then the dashboard is loopback-only, and so is the gateway.
- `pnpm audit --prod`: no known vulnerabilities. The build writes a **CycloneDX 1.7 SBOM** of the production
  dependencies with pnpm's own `sbom` (not served; it sits beside `dist/`).
- Nothing the model returns is stored as a fact about a customer: a prediction is a **suggestion event** —
  model revision, input, probabilities, and the human's answer.
- **pnpm's minimum release age** refused `prettier@3.9.9` (published hours earlier); the pin is 3.9.8 rather
  than an exemption. pnpm reports `eslint@9.39.5` **deprecated** in favour of 10; the requirement pins ESLint 9,
  so it stays, and the major bump is a deliberate PR.

## Testing

- **Unit (Vitest + React Testing Library), 80 tests:** the 22 shared schema cases; the probability renderer
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
  the pair back through the API and shows a second correction refused with the first standing.
- Not covered: model quality, which is the eval panel's number rather than a test.

## Evidence

Under [`evidence/`](evidence/). **Every screenshot shows the stub worker** and says so on the page, in red.

| file | what |
| --- | --- |
| `1-answer-view-stubbed.png` | per-option probabilities, top-2 and confidence for a three-question schema |
| `2-correction-captured-stubbed.png` | the recorded pair, accepted and corrected per question |
| `3-eval-panel-stubbed.png` | a synthetic labelled CSV beside the majority-class baseline |
| `e2e-transcript.txt` | the Playwright run |
| `headers-and-ports.txt` | `curl -skI` of the served page and `ss -ltn` |

## Open questions

- [ ] **Q:** ratify or reject the DIP response-shape change. Not assumed here. — *owner:* Dhira
- [ ] **Q:** what "user" means on a single-account box: the gate on auth, and on a hostname. — *owner:* Dhira
- [ ] **Q:** retention and access for the correction store: personal data and the training set. — *owner:* Dhira
- [ ] **When the worker lands:** replace the stub contract in `decisions/worker.go` with the real answer shape,
  and run the e2e against the worker instead of the stub.
