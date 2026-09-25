# [2] The inferences console: what it should be

Status: **for review before build.** This doc is the research that precedes the work Dhira asked for
(2026-09-25): refocus the inferences dashboard as an internal tool with a menu per inference service, show
what is ready and what is busy, view and edit logs, see all models, turn a service on and off, and prepare
for OCR, ASR and TTS. It also revisits three non-goals of `[1]technical-requirement.md` (no training, no
registry UI, no LAN exposure before auth), because asking for them is asking for those non-goals to change.

House style applies: stdlib-first, minimal dependencies, no new backend where a link will do.

## 1. What this is, and what it is not

- **It is** the operator's console for the inference services on this box: what is running, with which
  model, how loaded, how busy, what it has been doing, and (later) how to change what is resident.
- **It is not** the Dita OS cockpit. That is a separate, later product that will connect to the inferences
  services the same way this console does, and also carry the personal-assistant side. Building the cockpit
  in this tool is the "not a second front end" non-goal, and it stays.
- **It is not** a configuration editor for compose, Caddy or the host. Restarting a container stays a host
  action.
- **Read-only first.** Release 1 observes. Control (§10) is specified and sequenced, not smuggled in, and
  nothing in release 1 may unload, reconfigure or restart anything.

## 2. What exists today, and the three facts that shape every choice

Full inventory is in the evidence of this doc's PR; the load-bearing facts:

**Fact 1 — a worker holds exactly ONE model, and swapping it is a release-then-load.**
`packages/pylibs/worker/src/worker/manager.py:1-8` releases the previous engine before building the next, and
downloads first so a failed fetch leaves the working model serving. So "turn embedding off" cannot mean
"turn off one of several"; it means **nothing is resident** and every request gets `503 Unhealthy`
(`embedding_worker/tei.py:77-80`). RAM is freed only when nothing is resident.

**Fact 2 — runtime load/unload EXISTS in the protocol and has NO HTTP route.**
DIP ops `list`, `load`, `unload` are dispatched (`worker/server.py:40-43,104-108`) and the sockets are bound
in `run/`. But no service exposes them over HTTP (`embedding_worker/tei.py:77`, `reranker_worker/tei.py:78`,
`system_one_worker/decide.py:105` are inference-only), and the orchestrator has no DIP client at all
(`packages/golibs/dip` is not imported anywhere under `services/dita-orchestrator`). **Today, the only way to
change a resident model is a hand-run DIP client or a container restart.** Any "on/off" button needs new
server work first. This is the single biggest scope fact in the document.

**Fact 3 — the status vocabulary already exists, and is better than what a UI would invent.**
`GET /api/inferences/workers` aggregates each worker's `/health` and `/info` and answers
`{workers:[{name,url,state,health_status,info,error?}]}` with
`state ∈ {ready, no_model, unhealthy, not_running, busy, timeout, unreachable, client_gone}`
(`handler/inferences/workers.go:13-65`). `no_model` and `not_running` are exactly the two different "off"s
that HF Inference Endpoints separates as *paused* vs *scaled-to-zero*; the console must not flatten them.

| service | language | HTTP surface today | resident model | notes |
|---|---|---|---|---|
| `inferences-embedding` | Python 3.14 | `POST /embed`, `GET /info`, `GET /health`, `GET /metrics` | `qwen3-embedding-0.6b` (int8, 1024) | 3 models registered in `models.yaml` |
| `inferences-reranker` | Python 3.14 | `POST /rerank`, `GET /info`, `GET /health`, `GET /metrics` | `jina-reranker-v1-turbo-en` | `qwen3-reranker-0.6b` registered as rollback; multilingual jina commented |
| `inferences-system-one` | Python 3.14 | `POST /decide`, `GET /info`, `GET /health`, `GET /metrics` | `laya-multilingual` | one request at a time; `max_planned_tokens` 8192 |
| `inferences-ocr` | Python 3.14 | **none** (`Worker(...)` built without `routes=`) — `/metrics` only | `rapidocr`/`tesseract`/`manga_ocr` over DIP | not running |
| `inferences-stt`, `inferences-tts` | — | **do not exist** (`.gitkeep` only) | — | as instructed: not turned on |

Worker metrics are real and rich — `dita_worker_ops_total`, `dita_worker_errors_total`,
`dita_worker_model_loads_total`, `dita_worker_model_evictions_total`, histograms
`dita_worker_load_duration_seconds` / `dita_worker_infer_duration_seconds`, and gauges
`dita_worker_model_resident`, `dita_worker_model_resident_seconds`, `dita_worker_model_loading`,
`process_resident_memory_bytes` (`worker/metrics.py:171-232`) — but **nothing scrapes them**: there is no
Prometheus, no Grafana and no Loki on this host. They are published and uncollected.

**Absent, verified:** no metrics on the orchestrator; no log API anywhere (logs are `docker logs` or Dozzle at
`dozzle.home.arpa`); no runtime load/unload/select over HTTP; no training, fine-tuning, LoRA/PEFT or dataset
export code; no auth on reads and none on writes by default (`INFERENCES_API_TOKEN` defaults empty,
`services/dita-orchestrator/compose.yaml:27`); no streaming transport (no SSE, no WebSocket).

## 3. Precedent: what to copy, what to refuse

| source | copy | refuse |
|---|---|---|
| TEI (`/health`, `/info`, `/metrics`) | the three responsibilities, separated: liveness, identity, time series — we already have all three, so the console reads them rather than inventing | — |
| HF Inference Endpoints | **two kinds of "off"**: *paused* (never wakes on its own) vs *scaled-to-zero* (wakes on a call, with a cold start you can feel). Vendor polls state every 5 s | — |
| LM Studio (`/api/v1/models`, `/load`, `/unload`, download status) | three object types with different lifetimes: **artifact** (on disk), **runtime** (resident), **job** (download/load in progress) | — |
| Ollama (`/api/ps`) | model as a **lease with an expiry** (`expires_at`), and the footprint facts (`parameter_size`, `quantization_level`, `size_vram`) | unload as a one-click action: the API exposes no in-flight count, so a console cannot say what it would drop |
| Ray Serve | `DRAINING` as a *policy* state ("healthy but closed to new requests"); every state carries a human `message`; "busy" as a **number**, not a colour | percentiles computed from pre-bucketed histograms — Ray's own dashboard docs warn the buckets limit accuracy |
| vLLM | a documented metric namespace | any "proxy any endpoint" console surface: the vendor marks its control endpoints dev-mode-only and says **do not expose them** |
| Grafana | "choose the slowest refresh interval that meets your requirements"; hierarchy with drill-down instead of one dashboard per entity; RED for services, USE for hardware; don't stack misleading series | dashboard sprawl, in-browser editing |
| Beszel / Dozzle (already on this box) | link out to them for host metrics and raw container logs instead of rebuilding both | — |

## 4. Navigation and information architecture

**Decision: top-level sections are kinds of thing; services are rows, not navigation items.**

- Sidebar, five entries: **Fleet** · **Models** · **Activity** · **Jobs** · **Console settings**.
  Fleet is the landing page.
- **Fleet** lists every service as a row with: name, state word, resident model, resident-for,
  in-flight/queue if known, last error, and a link to its detail page.
- A service's menu (the per-service thing Dhira asked for) is its **detail page's tabs**, addressed by route
  `/services/:id` — Overview · Models · Metrics · Logs · Try it. Adding the 11th service costs no navigation
  change; one-nav-item-per-service is the sprawl every precedent above warns about.
- `inferences-ocr`, `inferences-stt`, `inferences-tts` appear as **rows with state `not deployed`** and a
  reason ("no HTTP surface: DIP only", "no code"). They get no tabs and no actions. This is the recommendation
  on the fleet-vs-existing question: show the intended fleet so the shape is visible, but let the state say
  what is real, and never render a button that cannot work.

**Two levels, not three** (NN/g: beyond two disclosure levels usability falls off). Fleet → service detail.
Within a detail page, raw JSON, full metric lists and load/unload controls live behind an expander or a modal,
not on another page.

## 5. A service detail page

- **Overview** — the state word with its message, resident model and how long, the model's identity from
  `/info` (`model_id`, `model_sha`, `dimensions`/`max_input_length`, `max_concurrent_requests`,
  `max_batch_tokens`), throughput and error counts from `/metrics`, RAM now and peak, and the blast radius
  ("Hindsight recalls and consolidation use this").
- **Models** — every model in the service's `models.yaml`: id, pinned revision and sha256, languages,
  on-disk size, and whether it is resident / on disk / not downloaded. Only one can be resident (Fact 1), and
  the page says so instead of rendering N toggles.
- **Metrics** — the `dita_worker_*` series this worker actually emits, rendered as series with units, plus the
  honest note that nothing collects them historically. No history chart until something stores history.
- **Logs** — see §8.
- **Try it** — a request form per service (`/embed`, `/rerank`, `/decide`) that posts through the gateway and
  shows the raw response, timings from `x-compute-time`, and the refusal verbatim when refused. This is the
  single most useful page for guiding inference work, and it needs no new server capability.

## 6. The status vocabulary

Nine states, each a **word + colour + shape + message**, never colour alone (WCAG 1.4.1).

| shown | source | means | the honest sentence |
|---|---|---|---|
| `ready` | `state: ready`, `resident` set | serving | "qwen3-embedding-0.6b, resident 3 h" |
| `busy` | `state: busy`, or in-flight > 0 | reachable, at its connection cap | "busy · 4 in flight · requests are refused, not queued" |
| `loading` | DIP `readyz.loading`, `dita_worker_model_loading` | fetching/building a model | "loading qwen3-reranker-0.6b" |
| `no model` | `state: no_model` | alive, nothing resident; requests get 503 | "alive, not serving · frees RAM · requests fail until a model is loaded" |
| `stopped` | `state: not_running` | container down | "container not running — a host action, not something this console can change" |
| `unhealthy` | `state: unhealthy` | its own `/health` fails | the worker's own error text |
| `unreachable` / `timeout` | same | network or deadline | as reported |
| `degraded` | gateway `client_gone` | the caller vanished | as reported |
| `not deployed` | not in the compose at all | no code or no HTTP surface | the reason, e.g. "OCR is DIP-only" |

**`busy` is a number wherever we have one.** Our workers refuse rather than queue (`textinfer/tei.py:37-58`),
so "busy" is not a backlog — say "requests are being refused", which is what the operator needs to know.

## 7. Live updates

**Decision: plain polling, with the interval chosen per screen, and no SSE/WebSocket.**

- Fleet: every **5 s** while visible (the cadence HF Inference Endpoints uses for state transitions).
- Service detail: every **10 s**; metrics block every **30 s**.
- **Stop polling when the tab is hidden** (`visibilitychange`) and show `as of HH:MM:SS` next to every
  timestamp, so a stale screen is never read as live.
- No streaming: the gateway has no SSE or WebSocket code, and adding one to show numbers that change every
  few seconds is not worth a new transport. Revisit only if logs are ever streamed to the browser.

Grafana's rule applies literally: the slowest interval that meets the need. A model that has been resident for
three hours does not change every 200 ms.

## 8. Logs

**Decision: link out to the tool we already run; build no log store.**

- The Services page links each service to Dozzle (`dozzle.home.arpa`, already deployed) filtered to that
  container, and to `docker logs` instructions for the terminal. A new tab, not an embedded iframe.
- We give up in-page filtering and log retention. That is the correct trade: a broker plus a store plus
  retention is a maintenance surface Dhira has ruled out, and Dozzle already answers "what did it just say".
- Note for whoever wires it: the Python workers log **plain text** (`cli.py:116-119`) and the orchestrator logs
  **JSON**; a future embedded viewer must not promise structured filtering over both.
- The orchestrator's own request log is the exception worth surfacing: it is JSON with method, path, status and
  duration, and a "recent refusals" panel on Fleet (from the gateway's own log, once there is a log API) is a
  release-3 idea, not release 1.

## 9. Metrics

- **Read what exists.** Worker `/metrics` is Prometheus text on the worker's own port; the browser cannot reach
  those ports, so the gateway needs a small read-only pass-through (`GET /api/inferences/metrics/{service}`)
  that returns the text verbatim, guarded like the other reads. Parse and display a fixed, named subset — the
  `dita_worker_*` names in §2 — not "whatever is in the text".
- **Do not compute percentiles in the browser from histogram buckets.** Prometheus's own guidance is that
  quantile error is bounded by bucket width and should be computed at query time; a client-side p95 from
  buckets is exactly the confident-but-wrong number this doc forbids. Show the buckets, or show the count and
  the sum and label the derived average as an average.
- **No history.** Counters are in-memory and reset on restart (`worker/metrics.py:84-100`); say "since the last
  restart" wherever a total is shown, and do not draw a trend line from two samples.
- Host metrics stay in Beszel, which is already deployed and does it better. Do not rebuild them.

## 10. Turning a service on and off (the control surface)

**The gap first: it cannot be done today.** Fact 2 — no HTTP route, no DIP client. So this is new work, and it
is sequenced, because a control plane is where a homelab console does damage.

**Two different "off", kept distinct (copying HF Endpoints):**

1. **Unload** — no model resident, the process alive, requests answer `503 Unhealthy` fast. Frees the model's
   RAM (~3 GiB for the embedding model, ~0.3 GiB for the reranker), keeps the service reachable, and the next
   load is a fetch-free rebuild. This is the one worth having for a 16 GB box.
2. **Stop** — the container is stopped. Frees everything including the interpreter, and the service is
   `not_running`. Doing this from the console means the console can start processes it does not own: **not in
   scope.** The console shows `stopped` and tells Dhira the compose command.

**Server work this needs, in order:**

- **a. Explicit worker routes.** `POST /load` `{model_id}`, `POST /unload`, `GET /models` per service,
  mirroring LM Studio's shape, added to each worker's `routes` table. Load/unload stays the worker's own
  decision, so its release-before-load ordering (Fact 1) is preserved.
- **b. The gateway becomes the only client.** New routes `POST /api/inferences/{service}/load|unload` and
  `GET /api/inferences/{service}/models`, guarded by the same write guard as every other POST, and **never** a
  generic proxy of arbitrary worker paths (vLLM's warning).
- **c. In-flight visibility before the button is enabled.** The UI reads in-flight from the metrics
  pass-through and states the consequence: with one concurrency slot in system-one, an unload during a request
  drops that request; the button says so, and stays disabled while a request is in flight.
- **d. An audit record.** Every load/unload appends `{at, actor, service, action, model, outcome}` to an
  append-only file next to the decisions store, and the console shows the last few. Kubernetes' audit record
  fields (who, when, what, verb) are the model; there is no homelab-scale UI convention to copy, so keep it to
  a table.
- **e. Typed confirmation naming the model and the blast radius** — "Unload `qwen3-embedding-0.6b`? Hindsight
  recall and consolidation will fail until a model is loaded." For a *destructive* action, a confirmation
  dialog is justified; for routine loads it would be noise (NN/g: confirmations are for consequences, not for
  every action).

**Two things the console must never do:** load a model the registry does not pin (every file is revision- and
sha256-pinned, `worker/registry.py:161-170`), and offer a "restart" button.

## 11. Training and fine-tuning

**Nothing in the repo trains, fine-tunes, fits or exports anything** (verified by grep across `services/`,
`packages/`, `docs/`, `specs/`). `[1]technical-requirement.md` lists training as a non-goal; the store holds
the pairs a future fit would need, and system-one's checkpoints ship with every temperature at 1.0.

**Decision: build no training UI until a job runner exists.** A progress bar over a non-existent job is the
dishonest version of this feature. What ships in release 1 instead:

- **Data**: the pairs are visible (`/api/inferences/decisions`, `/evaluations`, `/stats`) and exportable as
  JSONL from the store's own files.
- **A Jobs page that is honest**: it lists real things only. In release 1 that means evaluations (uploaded and
  scored, with their real numbers) and nothing else; a "training" row appears when a trainer does.
- **The seam**: when a fit exists, it is a job with `{id, kind, state ∈ pending/running/succeeded/failed/stopped,
  started_at, ended_at, progress, log ref, artifact}`, polled like everything else, with the artifact link
  being the point. Ray's `JobStatus` is the vocabulary; `is_terminal()` is why the poll stops.

**Requirement (Dhira, 2026-09-25): capture every inference's input and output, so a future fit trains on our own
usage.** Nothing captures it today. What each service would have to keep, read from the live stores on
2026-09-25:

- **Reranker — nothing is kept.** Each recall sends a query plus up to 32 candidates to `inferences-reranker`
  and discards the scores. Training a reranker on real usage needs a new store of `(query, candidates, scores)`
  plus which result was actually **used** — the last part is what turns a score into a label.
- **Embedding — half of it.** Hindsight keeps each memory's text and its vector (`memory_units.embedding`, ~3,000
  rows) and the graph around it (`memory_links` ~34,000, `entity_cooccurrences` ~13,000), but **no recall query
  is stored anywhere**, and a stored vector carries **no model identity** — so swapping the embedder is
  invisible in the data, which is precisely the change that is one-way.
- **system-one — the model to copy.** The orchestrator's decisions store keeps `input_text` forever, append-only,
  for this exact reason; an answer plus any human correction is the pair.
- **Hindsight's own `llm_requests`** (504 rows) keeps its extraction model's prompt/response JSON. That is the
  extraction LLM, not the retriever — do not mistake it for retrieval training data.
- **Workers and gateway keep nothing**: both workers mount only their model weights and the DIP sockets.

Constraints any implementation must honour: an asynchronous write off the hot path that never blocks or slows a
request; a stated size bound and retention; the model id **and** revision recorded with every record so a swap is
visible; no second source of truth for the services it observes; and a per-inference on/off flag the console can
show. OCR, ASR and TTS follow the same rule when they exist.

## 12. OCR, ASR and TTS

Present as intended rows in Fleet, with the truth in the state:

- **OCR** — code exists and works over DIP with three engines, but the worker is built without `routes=`, so it
  has no HTTP surface; the console can show it as `not deployed · DIP only`, and the gateway doc already carries
  "a DIP client over the shared socket and an OCR row in `/workers`" as an open question.
- **ASR and TTS** — empty directories. `not deployed · no code`. Per Dhira: not turned on.
- Nothing in the console offers an action for these three, and the Try-it tab is absent for them. When one
  lands it gets the same tabs as everything else; that is the point of the shape.

## 13. UI and UX rules

- **Colour is never the only signal** (WCAG 1.4.1): word + colour + shape for every state; status text meets
  4.5:1 contrast (1.4.3); every focusable element has a visible focus ring (2.4.7); tap targets ≥24×24 CSS px
  (2.5.8).
- **Reflow at 320 px** (1.4.10): the fleet table becomes one card per service with the state in the header. A
  six-column table on a phone is not a table.
- **>5 comparable rows → table; ≤5 heterogeneous objects → cards.** Fleet is a table; a service's models are
  cards.
- **Progressive disclosure, two levels**, and raw JSON only behind an expander. A page that shows the
  worker's `/info` verbatim is a page nobody reads.
- **Response-time honesty** (NN/g's 0.1 / 1 / 10 s): anything that can take longer than a second — a load, a
  decision, an evaluation — shows progress with the elapsed time, and never a spinner with no clock.
- **Empty, loading and error states are designed, not defaulted**: "no model resident" is a designed empty
  state, and every refusal from a worker is shown verbatim with its `error_type`, because the workers already
  say precisely what is wrong.
- Dark and light both, following the system preference with an explicit override.

## 14. Performance

The console is a static SPA on the LAN; the budget is small because everything it needs already exists.

- **Route-level code splitting** (`React.lazy` + `Suspense`) so Fleet — the landing page — does not carry the
  Try-it form, the metrics parser or the table library.
- **Bundle budget: ≤170 KB compressed critical path** (web.dev's figure for a 3G baseline), enforced by the
  existing Vite build; note Vite's `chunkSizeWarningLimit` (default 500 kB) measures *uncompressed* chunks, so
  the two numbers are not comparable — track the built `dist/` size in the PR and keep it falling.
- **No dashboard-library dependency** for charts in release 1: there is nothing to chart yet (§9), and a chart
  library is the largest single thing we could add for no data.
- **A polling table must not re-render its rows**: subscribe with `useSyncExternalStore` (or `memo` with stable
  props) so a 5 s tick updates the cells that changed, not the tree. TanStack Table's React Compiler guide is
  the reference for the table case.
- **Measure, don't assert**: `pnpm build` chunk sizes per PR, and Lighthouse against `inferences.home.arpa`
  (LCP 25 %, TBT 30 %, CLS 25 % of the score) in the PR body with a screenshot. The repo's
  `scripts/check-dist.mjs` key-shape scan stays in the build.

## 15. Deliberately not building

No Prometheus/Grafana/Loki deployment; no log store or embedded log viewer; no history for metrics that live in
memory; no generic worker-path proxy; no training or fine-tuning UI before a trainer exists; no "restart
container" button; no per-service navigation entries; no second front end for the Dita OS cockpit; no auth
inside the SPA (the gate belongs in Caddy — see §17); no WebSocket/SSE transport; no percentile computed from
histogram buckets in the browser.

## 16. Rollout, and its rollback

The console is static files served by the host Caddy from `services/inferences-dashboard/dist`
(`~/docker/caddy/compose.yaml:32`), so shipping it is `pnpm build` on `main`. Rollout is therefore cheap and
reversible: keep the previous `dist/` aside before the build, and restore it to roll back. New server routes
(§10) are the only part with a real rollback story, and they ship behind the write guard and the token.

Suggested order: (1) the console's shape on read-only data — Fleet, detail tabs, Try-it, metrics pass-through;
(2) control — worker routes, gateway routes, audit record, in-flight gating; (3) whatever the Jobs page has
earned by then.

## 17. Open questions

- [ ] **Q:** the three non-goals this doc revisits — "no training, no registry UI, no LAN exposure before
  auth" — are now requested features. Which are ratified, and does auth come before any of them? — *owner:* Dhira
- [ ] **Q:** is control (§10) in scope for the first release, or does it wait for release 2 as suggested? — *owner:* Dhira
- [ ] **Q:** the Caddy/token decision (already open in the gateway doc): `orchestrator.api.home.arpa` still
  publishes every route, and `INFERENCES_API_TOKEN` is unset in the deployment. A page whose buttons unload a
  model raises the stakes on both. — *owner:* Dhira
- [ ] **Q:** Dozzle already has a login (`dozzle-users.yml`) and this console has none. Is the console meant to
  sit behind the same gate? — *owner:* Dhira
- [ ] **Q:** should the repo have CI (the dashboard's shared schema cases sat failing on `main` with nothing to
  catch them)? It would run `pnpm test`, `pnpm lint` and `go test ./...` on push. — *owner:* Dhira

## 18. Verification checklist

- Fleet shows every service with a state word and a message, and no state is conveyed by colour alone.
- `no model` and `stopped` are distinguishable, and neither is labelled "off".
- No button exists for a service whose state says `not deployed`, and no action appears where the server
  capability is absent.
- Polling stops when the tab is hidden, and every timestamp says `as of`.
- Try-it shows the worker's refusal verbatim, with `error_type`.
- A metric total is labelled "since the last restart"; no percentile is computed from buckets client-side.
- The 320 px view reflows the fleet table to cards.
- Every load/unload (release 2) writes an audit line, names the model, states the blast radius, and is disabled
  while a request is in flight.
- `pnpm build` size and a Lighthouse screenshot are attached to the PR that changes the SPA.
